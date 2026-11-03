"""Go-live loading (Phase 7): the real Employee Master workbook and opening leave balances.

Both work in two steps: read the file and show exactly what will happen (new,
update, same, error) without saving anything, then the HR Manager applies it.
Between the two steps the parsed rows wait in a private file under
STORAGE_DIR/imports (deleted on apply, or after a day), never in the page,
because the Restricted sheet holds ID card and work permit numbers.

Loading the master is for go-live. Afterwards every change goes through the
normal requests (second HR check, job events)."""
from __future__ import annotations

import io
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Employee, EmployeeRestricted, EmploymentEvent, Grade, Job, OrgUnit, User
from ..models.core import EMPLOYEE_GROUPS, EXIT_REASONS
from ..models.hrdev import LeaveOpening, LeaveType
from . import people as P
from .common import audit, normalize_employee_id


class ImportError_(Exception):
    pass


# Employee_Master_v1.xlsx, sheet Master, row 1 headers -> app field
MASTER_COLS = {
    "Employee ID": "employee_id", "Employee Group": "employee_group", "Pay Type": "pay_type", "Title": "title", "Gender": "gender",
    "Full Name (EN)": "full_name_en", "Full Name (TH)": "full_name_th", "Nickname": "nickname", "Nationality": "nationality",
    "Birth Date": "birth_date", "Division": "_division", "Department": "_department", "Section": "_section", "Team": "team",
    "Position": "_position", "Position Level": "_level", "Join Date": "join_date", "Current Position Since": "position_since",
    "Status": "_status", "Exit Date": "exit_date", "Exit Reason": "_exit_reason", "Previous Employee ID": "previous_employee_id",
    "Residence Room": "_room", "Remark": "remark",
}
RESTRICTED_COLS = {"Employee ID": "employee_id", "ID Card / Passport No.": "_id_no", "Work Permit No.": "work_permit_no",
                   "Work Permit Expiry": "work_permit_expiry", "Disability Registered": "_disability"}
EXIT_TEXT = {
    "resigned voluntarily": "RES-OTHER", "resigned": "RES-OTHER", "contract ended": "CONTRACT", "terminated": "TERMINATED",
    "retired": "RETIRED", "passed away": "DECEASED", "transfer to staff": "TO-STAFF", "transfer to group company": "TO-GROUP",
    "other": "RES-OTHER", "not recorded": "NOT-RECORDED", "did not pass probation": "PROBATION",
}
# Fields an update may set directly at go-live. Org unit, job, grade and group differences are shown, not applied:
# after go-live they change through a job event with approvals.
UPDATABLE = [f for f in P.EDITABLE if f not in P.EVENT_FIELDS and f not in ("shift_code", "calendar_override", "work_email",
                                                                            "probation_end_date", "contract_end_date", "full_name_my")]


def _store() -> Path:
    d = settings.storage_dir / "imports"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for f in d.glob("*.json"):  # nothing waits longer than a day
        if now - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
    return d


def save_pending(kind: str, payload: Any) -> str:
    token = secrets.token_hex(12)
    (_store() / f"{kind}-{token}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str))
    return token


def load_pending(kind: str, token: str) -> Any:
    if not re.fullmatch(r"[0-9a-f]{24}", token or ""):
        raise ImportError_("This preview has expired. Upload the file again")
    p = _store() / f"{kind}-{token}.json"
    if not p.exists():
        raise ImportError_("This preview has expired. Upload the file again")
    data = json.loads(p.read_text())
    p.unlink(missing_ok=True)
    return data


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        v = " ".join(v.split())
        return v or None
    return v


def _date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return date(d.year - 543, d.month, d.day) if d.year > 2400 else d  # Buddhist year typed by hand
        except ValueError:
            continue
    raise ValueError(s)


def _sheet(wb, name: str, cols: dict) -> list[dict]:
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = ws.iter_rows(values_only=True)
    header = [(_cell(h) or "") for h in next(rows, [])]
    idx = {cols[h]: i for i, h in enumerate(header) if h in cols}
    out = []
    for n, r in enumerate(rows, start=2):
        rec = {k: _cell(r[i]) if i < len(r) else None for k, i in idx.items()}
        if not any(v not in (None, "") for v in rec.values()):
            continue
        if isinstance(rec.get("employee_id"), str) and rec["employee_id"].startswith("="):
            continue
        rec["_row"] = n
        out.append(rec)
    return out


def read_workbook(content: bytes) -> tuple[list[dict], list[dict]]:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:
        raise ImportError_("This is not a readable .xlsx file") from exc
    if "Master" not in wb.sheetnames:
        raise ImportError_("No sheet named 'Master': use the Employee_Master_v1 workbook")
    master = _sheet(wb, "Master", MASTER_COLS)
    if not master:
        raise ImportError_("The Master sheet has no rows")
    return master, _sheet(wb, "Restricted", RESTRICTED_COLS)


# ---------------- planning ----------------

@dataclass
class RowPlan:
    row: int
    employee_id: str
    name: str
    action: str  # new / update / same / error
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changes: dict = field(default_factory=dict)  # field -> [old, new]
    data: dict = field(default_factory=dict)
    restricted: dict = field(default_factory=dict)
    status: str = "Active"
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None


@dataclass
class Plan:
    rows: list[RowPlan]

    @property
    def counts(self) -> dict:
        c = {"new": 0, "update": 0, "same": 0, "error": 0}
        for r in self.rows:
            c[r.action] += 1
        c["warnings"] = sum(1 for r in self.rows if r.warnings)
        return c

    def to_json(self) -> list:
        return [asdict(r) for r in self.rows if r.action in ("new", "update")]


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s*\(.*?\)\s*", "", (s or "")).strip().lower()


class _Lookup:
    def __init__(self, db: Session):
        units = db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True))).all()
        self.dept = {_norm(u.name_en): u for u in units if u.level == "department"}
        self.dept.update({u.code.lower(): u for u in units if u.level == "department"})
        self.section = {(_norm(u.parent.name_en) if u.parent else "", _norm(u.name_en)): u for u in units if u.level == "section"}
        self.jobs = {_norm(j.title_en): j.code for j in db.scalars(select(Job).where(Job.active.is_(True)))}
        self.grades = {g.code.lower(): g.code for g in db.scalars(select(Grade))}
        from ..models import Position

        self.unit_jobs: dict[str, list[tuple[str, str]]] = {}
        titles = {j.code: j.title_en for j in db.scalars(select(Job))}
        self.unit_pos: dict[str, list[tuple[str, str]]] = {}
        for p in db.scalars(select(Position).where(Position.active.is_(True), Position.job_code.is_not(None))):
            self.unit_jobs.setdefault(p.org_unit_code, []).append((p.job_code, _norm(titles.get(p.job_code))))
            self.unit_pos.setdefault(p.org_unit_code, []).append((p.position_id, p.job_code))

    def job(self, title: str, unit: Optional[OrgUnit]) -> Optional[str]:
        t = _norm(title)
        if t in self.jobs:
            return self.jobs[t]
        if unit is not None:  # "Operator" in the master, "Operator, Drying & Packing" in the app
            hits = {code for code, jt in self.unit_jobs.get(unit.code, []) if jt.startswith(t)}
            if len(hits) == 1:
                return hits.pop()
        return None

    def unit(self, dept: Optional[str], section: Optional[str]) -> Optional[OrgUnit]:
        if section:
            return self.section.get((_norm(dept), _norm(section)))  # a section that is not in the app is an error, not the department
        return self.dept.get(_norm(dept))

    def position(self, unit: OrgUnit, job_code: Optional[str]) -> Optional[str]:
        hits = [pid for pid, code in self.unit_pos.get(unit.code, []) if code == job_code]
        return hits[0] if len(hits) == 1 else None


def plan_master(db: Session, master: list[dict], restricted: list[dict]) -> Plan:
    lk = _Lookup(db)
    in_file: dict[str, int] = {}
    for rec in master:
        eid = normalize_employee_id(str(rec.get("employee_id") or ""))
        in_file[eid] = in_file.get(eid, 0) + 1
    rest = {normalize_employee_id(str(r.get("employee_id") or "")): r for r in restricted}
    out: list[RowPlan] = []
    for rec in master:
        eid = normalize_employee_id(str(rec.get("employee_id") or ""))
        rp = RowPlan(row=rec["_row"], employee_id=eid, name=rec.get("full_name_en") or "", action="error")
        out.append(rp)
        if in_file.get(eid, 0) > 1:
            rp.errors.append("Employee ID appears more than once in the file")
            continue
        data: dict[str, Any] = {}
        for k in ("employee_group", "pay_type", "title", "gender", "full_name_en", "full_name_th", "nickname", "nationality", "team", "remark"):
            v = rec.get(k)
            data[k] = str(v) if v is not None else None
        data["employee_id"] = eid
        if data.get("team"):
            data["team"] = data["team"].replace("Team", "").strip()[:2]
        prev = rec.get("previous_employee_id")
        data["previous_employee_id"] = normalize_employee_id(str(prev)) if prev else None
        for k in ("birth_date", "join_date", "position_since", "exit_date"):
            try:
                data[k] = _date(rec.get(k))
            except ValueError as e:
                rp.errors.append(f"{k.replace('_', ' ').capitalize()} is not a date | {e}")
        unit = lk.unit(rec.get("_department"), rec.get("_section"))
        if unit is None:
            rp.errors.append(f"Department or section not in Org & positions | {rec.get('_department') or '-'} / {rec.get('_section') or '-'}")
        else:
            data["org_unit_code"] = unit.code
        level = str(rec.get("_level") or "").strip()
        if level:
            g = lk.grades.get(level.lower())
            if g:
                data["grade_code"] = g
            else:
                rp.warnings.append(f"Position level is not a grade in the app: left empty | {level}")
        pos_title = rec.get("_position")
        if pos_title:
            j = lk.job(str(pos_title), unit)
            if j:
                data["job_code"] = j
                if unit is not None and lk.position(unit, j):
                    data["position_id"] = lk.position(unit, j)  # the one budgeted position for this job in this unit
            else:
                rp.warnings.append(f"Position is not in the job list yet: job left empty | {pos_title}")
        status = str(rec.get("_status") or "Active").strip()
        rp.status = "Active" if status.lower() == "active" else "Resigned"
        if rp.status != "Active":
            reason = str(rec.get("_exit_reason") or "").strip()
            code = EXIT_TEXT.get(reason.lower()) or (reason.upper() if reason.upper() in dict(EXIT_REASONS) else None)
            if not data.get("exit_date"):
                rp.errors.append("Left the company but has no exit date")
            rp.exit_reason = code or "NOT-RECORDED"
            if not code:
                rp.warnings.append("Exit reason missing or not in the list: saved as Not recorded")
            rp.exit_date = data["exit_date"].isoformat() if data.get("exit_date") else None
        elif data.get("exit_date"):
            rp.errors.append("Active but has an exit date")
        r = rest.get(eid) or {}
        restricted_data = {}
        idno = r.get("_id_no")
        if idno:
            idno = str(idno).replace("-", "").replace(" ", "")
            restricted_data["id_card_no" if re.fullmatch(r"\d{13}", idno) else "passport_no"] = idno
        if r.get("work_permit_no"):
            restricted_data["work_permit_no"] = str(r["work_permit_no"])
        try:
            if r.get("work_permit_expiry"):
                restricted_data["work_permit_expiry"] = _date(r["work_permit_expiry"])
        except ValueError as e:
            rp.errors.append(f"Work permit expiry is not a date | {e}")
        if r.get("_disability") and str(r["_disability"]).lower() in ("yes", "y", "true", "1"):
            restricted_data["disability"] = "Registered"
        if rec.get("_room"):
            restricted_data["residence_room"] = str(rec["_room"])[:20]
        rp.restricted = restricted_data
        if rp.errors:
            continue
        existing = db.get(Employee, eid)
        check = {k: v for k, v in data.items() if k not in ("position_since", "exit_date")}
        errs = P.validate(db, check, existing=existing)
        errs = [e for e in errs if not (e.startswith("Previous Employee ID") and data.get("previous_employee_id") in in_file)]
        if existing is None:
            rp.errors += errs
            if not rp.errors:
                rp.action = "new"
                soft = P.soft_checks(db, data) if rp.status == "Active" else []
                if restricted_data.get("work_permit_no") and restricted_data.get("work_permit_expiry"):
                    soft = [w for w in soft if not w.startswith("Foreign worker")]
                rp.warnings += soft
        else:
            rp.errors += [e for e in errs if "already exists" not in e]
            if rp.errors:
                continue
            for k in UPDATABLE:
                if k in data and data[k] is not None and data[k] != getattr(existing, k):
                    rp.changes[k] = [getattr(existing, k), data[k]]
            for k in ("org_unit_code", "job_code", "grade_code", "employee_group"):
                if data.get(k) and data[k] != getattr(existing, k):
                    rp.warnings.append(f"Differs from the app: change it with a job event | {k.replace('_code', '').replace('_', ' ')}: "
                                       f"{getattr(existing, k)} → {data[k]}")
            if rp.status != existing.status:
                rp.warnings.append("Status differs from the app: use the exit process, not the import")
            rp.action = "update" if rp.changes else "same"
        rp.data = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in data.items()}
        rp.restricted = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in restricted_data.items()}
    out.sort(key=lambda r: ({"error": 0, "new": 1, "update": 2, "same": 3}[r.action], r.row))
    return Plan(out)


def apply_master(db: Session, user: User, rows: list[dict]) -> dict:
    if not user.has("hr_manager"):
        raise ImportError_("Only the HR Manager loads the employee master")
    made = updated = 0
    news = [r for r in rows if r["action"] == "new"]
    # people referenced as Previous Employee ID first
    news.sort(key=lambda r: (r["data"].get("previous_employee_id") is not None, r["employee_id"]))
    for r in news:
        d = P.clean(dict(r["data"]))
        if db.get(Employee, d["employee_id"]):
            continue
        since = d.pop("position_since", None)
        d.pop("exit_date", None)
        emp = Employee(**{k: v for k, v in d.items() if hasattr(Employee, k)})
        emp.status = r["status"]
        emp.position_since = since or emp.join_date
        if r["status"] != "Active":
            emp.exit_date = P.parse_date(r["exit_date"])
            emp.exit_reason_code = r["exit_reason"]
        db.add(emp)
        db.flush()
        rest = P.clean(dict(r.get("restricted") or {}))
        if rest:
            db.add(EmployeeRestricted(employee_id=emp.employee_id, **{k: v for k, v in rest.items() if k in P.RESTRICTED}))
        db.add(EmploymentEvent(employee_id=emp.employee_id, event_type="rehire" if emp.previous_employee_id else "hire",
                               effective_date=emp.join_date, to_org_unit=emp.org_unit_code, to_position=emp.position_id, to_job=emp.job_code, to_grade=emp.grade_code,
                               to_group=emp.employee_group, note="Loaded from the Employee Master at go-live", created_by=user.label, applied=True))
        if emp.exit_date:
            db.add(EmploymentEvent(employee_id=emp.employee_id, event_type="exit", effective_date=emp.exit_date, from_org_unit=emp.org_unit_code,
                                   reason_code=emp.exit_reason_code, note="Loaded from the Employee Master at go-live", created_by=user.label, applied=True))
        made += 1
    for r in rows:
        if r["action"] != "update":
            continue
        emp = db.get(Employee, r["employee_id"])
        if emp is None:
            continue
        d = P.clean(dict(r["data"]))
        changes = {}
        for k in UPDATABLE:
            if k in d and d[k] is not None and d[k] != getattr(emp, k):
                changes[k] = [str(getattr(emp, k)), str(d[k])]
                setattr(emp, k, d[k])
        if changes:
            db.add(EmploymentEvent(employee_id=emp.employee_id, event_type="data_correction", effective_date=date.today(), changes=changes,
                                   note="Go-live load from the Employee Master", created_by=user.label, applied=True))
            updated += 1
        rest = P.clean(dict(r.get("restricted") or {}))
        if rest:
            er = emp.restricted or EmployeeRestricted(employee_id=emp.employee_id)
            for k, v in rest.items():
                if k in P.RESTRICTED and v and not getattr(er, k, None):
                    setattr(er, k, v)
            db.add(er)
    audit(db, user, "import_master", "employee", None, f"Employee Master loaded: {made} new, {updated} updated")
    return {"new": made, "updated": updated}


# ---------------- opening leave balances ----------------

OPENING_COLS = {"Employee ID": "employee_id", "Leave type": "leave_type", "Year": "year",
                "Days used before go-live": "used_before", "Vacation carried in": "carry_in", "Note": "note"}


def opening_template(db: Session, year: int) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Opening"
    head = ["Employee ID", "Name (for checking)", "Leave type", "Year", "Days used before go-live", "Vacation carried in", "Note"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DCE6F1")
    for e in db.scalars(select(Employee).where(Employee.status == "Active").order_by(Employee.employee_id)):
        for lt in ("VAC", "SICK", "PERS"):
            ws.append([e.employee_id, e.full_name_th or e.full_name_en, lt, year, None, None, None])
    for col, w in zip("ABCDEFG", (12, 32, 10, 8, 24, 20, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    help_ = wb.create_sheet("How to fill")
    for line in ["One row per person and leave type. Leave empty rows empty; they are skipped.",
                 "Days used before go-live: days taken this year on paper, before the app started (0.5 for a half day).",
                 "Vacation carried in: only for VAC, the days brought from last year (max per company rule). Empty = the app works it out.",
                 "Leave types: " + ", ".join(lt.code for lt in db.scalars(select(LeaveType).where(LeaveType.active.is_(True))))]:
        help_.append([line])
    help_.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def plan_opening(db: Session, content: bytes) -> list[dict]:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:
        raise ImportError_("This is not a readable .xlsx file") from exc
    if "Opening" not in wb.sheetnames:
        raise ImportError_("No sheet named 'Opening': download the template first")
    types = {lt.code for lt in db.scalars(select(LeaveType))}
    out, seen = [], set()
    for rec in _sheet(wb, "Opening", OPENING_COLS):
        used, carry = rec.get("used_before"), rec.get("carry_in")
        if used in (None, "") and carry in (None, ""):
            continue
        eid = normalize_employee_id(str(rec.get("employee_id") or ""))
        r = {"row": rec["_row"], "employee_id": eid, "leave_type": str(rec.get("leave_type") or "").upper(), "year": rec.get("year"),
             "used_before": None, "carry_in": None, "note": rec.get("note"), "errors": []}
        emp = db.get(Employee, eid)
        if emp is None:
            r["errors"].append("Employee not found")
        if r["leave_type"] not in types:
            r["errors"].append("Unknown leave type")
        try:
            r["year"] = int(r["year"])
        except (TypeError, ValueError):
            r["errors"].append("Year must be a number")
        try:
            r["used_before"] = float(used) if used not in (None, "") else 0.0
            r["carry_in"] = float(carry) if carry not in (None, "") else None
        except (TypeError, ValueError):
            r["errors"].append("Days must be numbers")
        else:
            if r["used_before"] < 0 or (r["carry_in"] or 0) < 0 or r["used_before"] > 366:
                r["errors"].append("Days out of range")
            if r["carry_in"] is not None and r["leave_type"] != "VAC":
                r["errors"].append("Carried-in days are for vacation only")
        key = (eid, r["leave_type"], r["year"])
        if key in seen:
            r["errors"].append("Same person, type and year twice")
        seen.add(key)
        if emp is not None and not r["errors"]:
            r["name"] = emp.full_name_en
        out.append(r)
    return out


def apply_opening(db: Session, user: User, rows: list[dict]) -> int:
    if not user.has("hr_manager", "hr"):
        raise ImportError_("Only HR loads opening balances")
    n = 0
    for r in rows:
        if r["errors"]:
            continue
        o = db.scalar(select(LeaveOpening).where(LeaveOpening.employee_id == r["employee_id"], LeaveOpening.year == r["year"],
                                                 LeaveOpening.leave_type == r["leave_type"]))
        if o is None:
            o = LeaveOpening(employee_id=r["employee_id"], year=r["year"], leave_type=r["leave_type"])
            db.add(o)
        o.used_before, o.carry_in = r["used_before"], r["carry_in"]
        o.note = (str(r["note"])[:200] if r.get("note") else None)
        o.loaded_by = user.label
        n += 1
    audit(db, user, "import_leave_opening", "leave", None, f"{n} opening balance line(s) loaded")
    return n


def readiness(db: Session) -> list[dict]:
    """A short go-live checklist computed from the data."""
    from sqlalchemy import func

    from ..models import Holiday, Position

    today = date.today()
    active = db.scalar(select(func.count()).select_from(Employee).where(Employee.status == "Active")) or 0
    sample = db.scalar(select(func.count()).select_from(Employee).where(Employee.is_sample.is_(True))) or 0
    no_pos = db.scalar(select(func.count()).select_from(Employee).where(Employee.status == "Active", Employee.position_id.is_(None))) or 0
    heads = db.scalar(select(func.count()).select_from(OrgUnit).where(OrgUnit.active.is_(True), OrgUnit.level == "department",
                                                                       OrgUnit.head_employee_id.is_(None))) or 0
    hol = db.scalar(select(func.count()).select_from(Holiday).where(Holiday.day >= date(today.year + (1 if today.month == 12 else 0), 1, 1))) or 0
    linked = db.scalar(select(func.count()).select_from(User).where(User.employee_id.is_not(None), User.active.is_(True))) or 0
    opening = db.scalar(select(func.count()).select_from(LeaveOpening).where(LeaveOpening.year == today.year)) or 0
    pos = db.scalar(select(func.count()).select_from(Position).where(Position.active.is_(True))) or 0
    return [
        {"ok": sample == 0, "text": "No SAMPLE people left", "value": sample},
        {"ok": active > 0, "text": "Active employees loaded", "value": active},
        {"ok": no_pos == 0, "text": "Active employees without a budgeted position", "value": no_pos},
        {"ok": heads == 0, "text": "Departments without a Department Head", "value": heads},
        {"ok": pos > 0, "text": "Budgeted positions", "value": pos},
        {"ok": hol > 0, "text": "Company holidays loaded for this year", "value": hol},
        {"ok": linked > 1, "text": "Logins linked to an employee", "value": linked},
        {"ok": opening > 0, "text": "Opening leave lines for this year", "value": opening},
    ]
