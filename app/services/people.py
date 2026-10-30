"""Employee master (M1): new hire, data change, and job events.

Every change goes through a second person (HR) or the event workflow, and is
stored as an Employment Event. The employee row is only a snapshot of the
latest applied events, so any past date can be rebuilt."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ApprovalRequest,
    Employee,
    EmployeeRestricted,
    EmploymentEvent,
    OrgUnit,
    Position,
    User,
)
from ..models.core import EMPLOYEE_GROUPS, EXIT_REASONS, NATIONALITIES, PAY_TYPES
from . import approvals
from .approvals import StepSpec
from .common import audit, diff, normalize_employee_id, valid_employee_id

EDITABLE = [
    "employee_group",
    "pay_type",
    "title",
    "gender",
    "full_name_en",
    "full_name_th",
    "full_name_my",
    "nickname",
    "nationality",
    "birth_date",
    "join_date",
    "probation_end_date",
    "contract_end_date",
    "org_unit_code",
    "position_id",
    "job_code",
    "grade_code",
    "team",
    "shift_code",
    "calendar_override",
    "previous_employee_id",
    "work_email",
    "remark",
]
DATE_FIELDS = {"birth_date", "join_date", "probation_end_date", "contract_end_date", "exit_date", "work_permit_expiry", "visa_expiry"}
RESTRICTED = ["id_card_no", "passport_no", "work_permit_no", "work_permit_expiry", "visa_expiry", "disability", "residence_room"]
# Fields that may only change through a job event, not a data correction.
EVENT_FIELDS = {"org_unit_code", "position_id", "job_code", "grade_code", "employee_group"}

EVENT_STEPS = {
    "transfer": ["from_head", "to_head", "hr", "gm"],
    "promotion": ["from_head", "hr", "gm"],
    "demotion": ["from_head", "hr", "gm"],
    "grade_change": ["from_head", "hr", "gm"],
    "group_change": ["from_head", "hr", "gm"],
    "probation_pass": ["from_head", "hr"],
    "probation_fail": ["from_head", "hr"],
    "exit": ["from_head", "hr"],
}


class PeopleError(Exception):
    pass


def parse_date(v: Any) -> Optional[date]:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            if d.year > 2400:  # Buddhist year typed by mistake
                d = d.replace(year=d.year - 543)
            return d
        except ValueError:
            continue
    raise PeopleError(f"Not a date: {s}")


def clean(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            v = " ".join(v.split())  # V13: extra spaces
            v = v or None
        if k in DATE_FIELDS:
            v = parse_date(v)
        out[k] = v
    if out.get("employee_id"):
        out["employee_id"] = normalize_employee_id(out["employee_id"])
    if out.get("previous_employee_id"):
        out["previous_employee_id"] = normalize_employee_id(out["previous_employee_id"])
    if out.get("work_email"):
        out["work_email"] = out["work_email"].lower()
    return out


def validate(db: Session, data: dict, existing: Optional[Employee] = None) -> list[str]:
    """Blocking checks at the door (blueprint section 6, V1-V13 of the step 2 master)."""
    errs = []
    merged = {f: getattr(existing, f) for f in EDITABLE + ["employee_id", "status", "exit_date"]} if existing else {}
    merged.update({k: v for k, v in data.items()})
    eid = merged.get("employee_id")
    group = merged.get("employee_group")
    if group not in EMPLOYEE_GROUPS:
        errs.append("Employee Group is required")
    if not valid_employee_id(eid or "", group):
        errs.append("Employee ID must be YTOP + 3 digits for operators, YTR + 3 digits for everyone else")
    if existing is None and eid and db.get(Employee, eid):
        errs.append(f"Employee ID {eid} already exists. IDs are never reused")
    for f, label in (("full_name_en", "Full name (EN)"), ("org_unit_code", "Org Unit"), ("join_date", "Join date"), ("birth_date", "Birth date"), ("nationality", "Nationality")):
        if not merged.get(f):
            errs.append(f"{label} is required")
    if merged.get("pay_type") and merged["pay_type"] not in PAY_TYPES:
        errs.append("Pay type must be Monthly or Daily")
    if merged.get("nationality") and merged["nationality"] not in NATIONALITIES:
        errs.append("Nationality not in list")
    bd, jd = merged.get("birth_date"), merged.get("join_date")
    if bd and jd:
        age = jd.year - bd.year - ((jd.month, jd.day) < (bd.month, bd.day))
        if age < 15:
            errs.append(f"Age at join is {age}. Thai law does not allow work under 15")
        if age > 70:
            errs.append(f"Age at join is {age}. Please check the birth date")
    if merged.get("org_unit_code") and not db.get(OrgUnit, merged["org_unit_code"]):
        errs.append("Org Unit not found")
    pos_id = merged.get("position_id")
    if pos_id:
        pos = db.get(Position, pos_id)
        if not pos:
            errs.append("Position not found")
        elif pos.org_unit_code != merged.get("org_unit_code"):
            errs.append("Position belongs to another Org Unit")
    prev = merged.get("previous_employee_id")
    if prev and not db.get(Employee, prev):
        errs.append(f"Previous Employee ID {prev} not found")
    if merged.get("gender") and merged.get("title"):
        if (merged["title"] == "Mr." and merged["gender"] != "M") or (merged["title"] in ("Ms.", "Mrs.") and merged["gender"] != "F"):
            errs.append("Gender does not match title")
    email = merged.get("work_email")
    if email:
        other = db.scalar(select(Employee).where(Employee.work_email == email))
        if other and other.employee_id != eid:
            errs.append("Work email already used by another employee")
    return errs


def soft_checks(db: Session, emp: Employee | dict) -> list[str]:
    """Warnings that do not stop the save (they become Data Health flags)."""
    g = (lambda k: emp.get(k)) if isinstance(emp, dict) else (lambda k: getattr(emp, k, None))
    out = []
    bd, jd = g("birth_date"), g("join_date")
    if bd and jd:
        age = jd.year - bd.year - ((jd.month, jd.day) < (bd.month, bd.day))
        if age < 18:
            out.append(f"Juvenile worker (age {age} at join): no OT, no holiday work, no night work")
    if g("nationality") in ("Myanmar", "Other"):
        out.append("Foreign worker: add work permit number and expiry on the restricted tab")
    pos_id = g("position_id")
    if pos_id:
        pos = db.get(Position, pos_id)
        if pos and (not pos.budgeted or pos.frozen):
            out.append("Position is not budgeted or is frozen")
        elif pos and pos.filled >= pos.seats:
            out.append(f"Position already has {pos.filled} of {pos.seats} seats filled")
    elif g("employee_group"):
        out.append("No Position yet")
    return out


# ---------- requests ----------


def request_create(db: Session, user: User, data: dict, restricted: Optional[dict] = None) -> ApprovalRequest:
    data = clean(data)
    errs = validate(db, data)
    if errs:
        raise PeopleError("; ".join(errs))
    payload = {"action": "create", "data": _json(data), "restricted": _json(clean(restricted or {}))}
    return approvals.submit(
        db,
        request_type="EMP_CHANGE",
        entity="employee",
        entity_id=data["employee_id"],
        title=f"New employee {data['employee_id']} {data.get('full_name_en', '')}",
        steps=[StepSpec("hr_check", role="hr")],
        user=user,
        org_unit_code=data.get("org_unit_code"),
        employee_id=None,
        payload=payload,
    )


def request_update(db: Session, user: User, emp: Employee, data: dict, note: str = "") -> ApprovalRequest:
    data = clean({k: v for k, v in data.items() if k in EDITABLE})
    moved = [k for k in data if k in EVENT_FIELDS and data[k] != getattr(emp, k)]
    if moved:
        raise PeopleError("Org Unit, Position, Job, Grade and Group change through a job event (transfer, promotion...), not a data edit")
    old = {k: getattr(emp, k) for k in data}
    changes = diff(old, data)
    if not changes:
        raise PeopleError("Nothing changed")
    errs = validate(db, data, emp)
    if errs:
        raise PeopleError("; ".join(errs))
    return approvals.submit(
        db,
        request_type="EMP_CHANGE",
        entity="employee",
        entity_id=emp.employee_id,
        title=f"Data change {emp.employee_id}: {', '.join(changes)}",
        steps=[StepSpec("hr_check", role="hr")],
        user=user,
        org_unit_code=emp.org_unit_code,
        employee_id=emp.employee_id,
        payload={"action": "update", "changes": changes, "note": note},
    )


def request_event(db: Session, user: User, emp: Employee, event_type: str, data: dict) -> ApprovalRequest:
    if event_type not in EVENT_STEPS:
        raise PeopleError("Unknown event type")
    data = clean(data)
    eff = parse_date(data.get("effective_date"))
    if not eff:
        raise PeopleError("Effective date is required")
    if eff < emp.join_date:
        raise PeopleError("Effective date is before the join date")
    if emp.status != "Active":
        raise PeopleError("Employee is not active")
    to_unit = data.get("to_org_unit") or emp.org_unit_code
    if event_type == "exit":
        if data.get("reason_code") not in dict(EXIT_REASONS):
            raise PeopleError("Exit reason code is required (it drives turnover analysis)")
    if event_type == "transfer" and to_unit == emp.org_unit_code and not data.get("to_position"):
        raise PeopleError("A transfer needs a new Org Unit or Position")
    if data.get("to_position"):
        pos = db.get(Position, data["to_position"])
        if pos is None:
            raise PeopleError("Target Position not found")
        if pos.org_unit_code != to_unit:
            raise PeopleError("Target Position belongs to another Org Unit")
    steps: list[StepSpec] = []
    for s in EVENT_STEPS[event_type]:
        if s == "from_head":
            steps.append(StepSpec("dept_head", head_of=emp.org_unit_code))
        elif s == "to_head":
            if to_unit != emp.org_unit_code:
                steps.append(StepSpec("receiving_head", head_of=to_unit))
        elif s == "hr":
            steps.append(StepSpec("hr_check", role="hr"))
        elif s == "gm":
            steps.append(StepSpec("gm", role="gm"))
    payload = {"action": "event", "event_type": event_type, **_json(data), "effective_date": eff.isoformat()}
    return approvals.submit(
        db,
        request_type="EVENT",
        entity="employee",
        entity_id=emp.employee_id,
        title=f"{event_type.replace('_', ' ').title()} {emp.employee_id} from {eff.isoformat()}",
        steps=steps,
        user=user,
        org_unit_code=emp.org_unit_code,
        employee_id=emp.employee_id,
        payload=payload,
    )


@approvals.on_close("EMP_CHANGE")
def _apply_change(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    if outcome != "approved":
        return
    p = appr.payload or {}
    if p.get("action") == "create":
        data = clean(p["data"])
        errs = validate(db, data)
        if errs:
            raise PeopleError("; ".join(errs))
        emp = Employee(**{k: v for k, v in data.items() if hasattr(Employee, k)})
        emp.status = "Active"
        emp.position_since = emp.join_date
        db.add(emp)
        db.flush()
        r = clean(p.get("restricted") or {})
        if any(r.values()):
            db.add(EmployeeRestricted(employee_id=emp.employee_id, **{k: v for k, v in r.items() if k in RESTRICTED}))
        db.add(
            EmploymentEvent(
                employee_id=emp.employee_id,
                event_type="rehire" if emp.previous_employee_id else "hire",
                effective_date=emp.join_date,
                to_org_unit=emp.org_unit_code,
                to_position=emp.position_id,
                to_job=emp.job_code,
                to_grade=emp.grade_code,
                to_group=emp.employee_group,
                approval_id=appr.id,
                created_by=appr.submitted_by,
                applied=True,
            )
        )
        audit(db, user, "create", "employee", emp.employee_id, "New employee approved")
        from .newstaff import open_onboarding

        open_onboarding(db, emp, user)
    elif p.get("action") == "update":
        emp = db.get(Employee, appr.entity_id)
        changes = p["changes"]
        for k, (_old, new) in changes.items():
            setattr(emp, k, parse_date(new) if k in DATE_FIELDS else new)
        db.add(
            EmploymentEvent(
                employee_id=emp.employee_id,
                event_type="data_correction",
                effective_date=date.today(),
                changes=changes,
                note=p.get("note"),
                approval_id=appr.id,
                created_by=appr.submitted_by,
                applied=True,
            )
        )
        audit(db, user, "update", "employee", emp.employee_id, ", ".join(changes), changes)


@approvals.on_close("EVENT")
def _apply_event(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    if outcome != "approved":
        return
    p = appr.payload or {}
    emp = db.get(Employee, appr.entity_id)
    ev = EmploymentEvent(
        employee_id=emp.employee_id,
        event_type=p["event_type"],
        effective_date=parse_date(p["effective_date"]),
        from_org_unit=emp.org_unit_code,
        to_org_unit=p.get("to_org_unit") or emp.org_unit_code,
        from_position=emp.position_id,
        to_position=p.get("to_position") or (emp.position_id if not p.get("to_org_unit") or p.get("to_org_unit") == emp.org_unit_code else None),
        from_job=emp.job_code,
        to_job=p.get("to_job") or emp.job_code,
        from_grade=emp.grade_code,
        to_grade=p.get("to_grade") or emp.grade_code,
        from_group=emp.employee_group,
        to_group=p.get("to_group") or emp.employee_group,
        reason_code=p.get("reason_code"),
        note=p.get("note"),
        approval_id=appr.id,
        created_by=appr.submitted_by,
    )
    db.add(ev)
    db.flush()
    if ev.event_type in ("exit", "probation_fail"):
        from . import exits

        exits.open_case(db, emp, ev.effective_date, ev.reason_code or ("PROBATION" if ev.event_type == "probation_fail" else None), user, appr.id)
    apply_due_events(db, date.today(), employee_id=emp.employee_id)
    audit(db, user, "event", "employee", emp.employee_id, f"{ev.event_type} effective {ev.effective_date}")


def apply_due_events(db: Session, today: date, employee_id: Optional[str] = None) -> int:
    q = select(EmploymentEvent).where(EmploymentEvent.applied.is_(False), EmploymentEvent.effective_date <= today)
    if employee_id:
        q = q.where(EmploymentEvent.employee_id == employee_id)
    n = 0
    for ev in db.scalars(q.order_by(EmploymentEvent.effective_date, EmploymentEvent.id)):
        emp = db.get(Employee, ev.employee_id)
        if ev.event_type == "exit":
            emp.status = "Resigned"
            emp.exit_date = ev.effective_date
            emp.exit_reason_code = ev.reason_code
        elif ev.event_type == "probation_fail":
            emp.status = "Resigned"
            emp.exit_date = ev.effective_date
            emp.exit_reason_code = "PROBATION"
        elif ev.event_type == "probation_pass":
            pass
        else:
            moved = (ev.to_org_unit, ev.to_position, ev.to_job, ev.to_grade) != (emp.org_unit_code, emp.position_id, emp.job_code, emp.grade_code)
            emp.org_unit_code = ev.to_org_unit or emp.org_unit_code
            emp.position_id = ev.to_position
            emp.job_code = ev.to_job
            emp.grade_code = ev.to_grade
            emp.employee_group = ev.to_group or emp.employee_group
            if moved:
                emp.position_since = ev.effective_date
        ev.applied = True
        n += 1
    db.flush()
    return n


def org_unit_on(emp: Employee, d: date) -> Optional[str]:
    """Where was this person on date d? Rebuilt from events, not the snapshot."""
    unit = None
    for ev in sorted(emp.events, key=lambda e: (e.effective_date, e.id or 0)):
        if ev.effective_date > d:
            break
        if ev.to_org_unit:
            unit = ev.to_org_unit
    return unit or (emp.org_unit_code if emp.join_date <= d else None)


def headcount_on(db: Session, d: date) -> dict[str, int]:
    out: dict[str, int] = {}
    for emp in db.scalars(select(Employee)):
        if emp.is_active_on(d):
            out[emp.employee_group] = out.get(emp.employee_group, 0) + 1
    return out


def _json(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}
