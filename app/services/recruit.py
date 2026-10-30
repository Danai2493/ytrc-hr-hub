"""Recruitment (blueprint M4).

Flow: a Department Head asks for people (manpower request) -> Department Head,
HR Manager and, outside the budget, the GM approve -> AI drafts the job ad from
the JD and HR edits it -> candidates come in by form, CV or a photo of the
paper application (AI reads it, HR checks) -> screening checks run on their own
(age, work permit, former employee, duplicates, PDPA consent) -> interviews
with a scorecard -> offer -> hire, which goes through the normal new-employee
approval and opens onboarding.

Pay is not handled here: the salary is agreed in the contract and set in Bplus."""
from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Application,
    ApprovalRequest,
    Candidate,
    Employee,
    EmployeeRestricted,
    Interview,
    Job,
    JobSkill,
    OrgUnit,
    Position,
    Requisition,
    User,
)
from ..models.core import EMPLOYEE_GROUPS, NATIONALITIES
from ..models.recruit import ACTIVE_STAGES, RECOMMEND, REJECT_REASONS, REQ_REASONS, SOURCES, STAGES, WITHDRAW_REASONS
from . import approvals
from .approvals import StepSpec
from .common import audit, get_setting, next_free_employee_id, next_ref, user_scope

HR = ("hr", "hr_manager")
CV_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
MAX_FILE = 10 * 1024 * 1024
REHIRE_BLOCK = ("TERMINATED",)
REHIRE_CHECK = ("PROBATION",)


class RecruitError(Exception):
    pass


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


# ---------- manpower requests ----------

def seats_free(db: Session, pos: Position, exclude_req: Optional[int] = None) -> int:
    """Budgeted seats not filled and not already asked for in another open request."""
    asked = sum(r.open_seats for r in db.scalars(select(Requisition).where(Requisition.position_id == pos.position_id,
                                                                          Requisition.status.in_(("pending", "open", "on_hold"))))
                if r.id != exclude_req)
    return pos.seats - pos.filled - asked if pos.budgeted and not pos.frozen else 0


def req_checks(db: Session, data: dict, today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    out = []
    pos = db.get(Position, data.get("position_id")) if data.get("position_id") else None
    n = int(data.get("headcount") or 1)
    if pos is None:
        out.append(finding("flag", "req_no_position"))
    else:
        if pos.frozen:
            out.append(finding("block", "req_frozen", pos=pos.position_id))
        free = seats_free(db, pos, data.get("id"))
        if data.get("reason") != "replacement" and n > free:
            out.append(finding("flag", "req_over_budget", n=n, free=max(free, 0)))
    if data.get("reason") == "replacement":
        rid = data.get("replaces_employee_id")
        emp = db.get(Employee, rid) if rid else None
        if emp is None:
            out.append(finding("block", "req_replace_who"))
        elif emp.status == "Active" and not emp.exit_date and not _exit_pending(db, emp):
            out.append(finding("flag", "req_replace_active", emp=emp.employee_id))
    nb = data.get("needed_by")
    if nb and nb < today:
        out.append(finding("block", "req_past"))
    elif nb and nb < today + timedelta(days=14):
        out.append(finding("info", "req_short_notice", days=(nb - today).days))
    job = db.get(Job, data.get("job_code")) if data.get("job_code") else None
    if job is not None and not job.purpose:
        out.append(finding("info", "req_no_jd", job=job.code))
    return out


def _exit_pending(db: Session, emp: Employee) -> bool:
    return db.scalar(select(ApprovalRequest.id).where(ApprovalRequest.request_type == "EVENT", ApprovalRequest.entity_id == emp.employee_id,
                                                      ApprovalRequest.status == "pending")) is not None


def request_people(db: Session, user: User, data: dict) -> Requisition:
    for k in ("org_unit_code", "job_code", "needed_by", "reason", "employee_group"):
        if not data.get(k):
            raise RecruitError(f"{k.replace('_', ' ').capitalize()} is required")
    if data["reason"] not in REQ_REASONS:
        raise RecruitError("Unknown reason")
    if data["employee_group"] not in EMPLOYEE_GROUPS:
        raise RecruitError("Unknown employee group")
    n = int(data.get("headcount") or 1)
    if not 1 <= n <= 50:
        raise RecruitError("Headcount must be 1 to 50")
    scope = user_scope(db, user)
    if not (scope.all or data["org_unit_code"] in scope.org_units):
        raise RecruitError("You can ask for people only for your own units")
    pos = db.get(Position, data.get("position_id")) if data.get("position_id") else None
    if pos is not None and pos.org_unit_code != data["org_unit_code"]:
        raise RecruitError("Position belongs to another Org Unit")
    checks = req_checks(db, data)
    if any(c["level"] == "block" for c in checks):
        raise RecruitError("; ".join(rule_en(c) for c in checks if c["level"] == "block"))
    within = not any(c["code"] in ("req_over_budget", "req_no_position") for c in checks)
    req = Requisition(
        ref=next_ref(db, "MR", date.today().year), org_unit_code=data["org_unit_code"], position_id=pos.position_id if pos else None,
        job_code=data["job_code"], grade_code=data.get("grade_code") or (pos.grade_code if pos else None) or db.get(Job, data["job_code"]).default_grade_code,
        employee_group=data["employee_group"], headcount=n, reason=data["reason"], replaces_employee_id=data.get("replaces_employee_id") or None,
        needed_by=data["needed_by"], within_budget=within, requirements=(data.get("requirements") or "")[:2000] or None,
        note=(data.get("note") or "")[:1000] or None, requested_by=user.label, status="pending",
    )
    db.add(req)
    db.flush()
    steps = [StepSpec("dept_head", head_of=req.org_unit_code), StepSpec("hr_manager", role="hr_manager")]
    if not within or req.reason == "new":
        steps.append(StepSpec("gm", role="gm"))
    appr = approvals.submit(db, request_type="REQUISITION", entity="requisition", entity_id=req.ref,
                            title=f"{req.ref} {n} x {req.job.title_en}, {db.get(OrgUnit, req.org_unit_code).name_en}",
                            steps=steps, user=user, org_unit_code=req.org_unit_code,
                            payload={"requisition_id": req.id, "checks": checks})
    req.approval_id = appr.id
    audit(db, user, "create", "requisition", req.ref, f"{n} x {req.job_code} for {req.org_unit_code} ({req.reason})")
    return req


@approvals.on_close("REQUISITION")
def _req_close(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    req = db.get(Requisition, (appr.payload or {}).get("requisition_id"))
    if req is None:
        return
    if outcome == "approved":
        req.status, req.opened_on = "open", date.today()
        if not req.ad_th and not req.ad_en:
            draft_ad(db, req)  # an AI failure leaves the ad empty; HR writes it
    else:
        req.status, req.closed_on = ("rejected" if outcome == "rejected" else "cancelled"), date.today()


def set_status(db: Session, user: User, req: Requisition, status: str, note: str = "") -> None:
    allowed = {"open": ("on_hold",), "on_hold": ("open",), "filled": ("open",)}
    if status == "cancelled":
        if req.status in ("filled", "cancelled", "rejected"):
            raise RecruitError("Already closed")
        if req.status == "pending" and req.approval_id:
            appr = db.get(ApprovalRequest, req.approval_id)
            if appr and appr.status == "pending":
                approvals.cancel(db, appr, user, note or "Manpower request cancelled")
        req.status, req.closed_on = "cancelled", date.today()
        for a in req.applications:
            if a.stage in ACTIVE_STAGES:
                move(db, user, a, "rejected", "Request cancelled", "filled", force=True)
    elif status in allowed.get(req.status, ()) or (status == "open" and req.status == "filled" and req.open_seats > 0):
        req.status = status
        req.closed_on = None
    else:
        raise RecruitError(f"Cannot change {req.status} to {status}")
    audit(db, user, "status", "requisition", req.ref, f"{status}. {note}"[:300])


AD_SCHEMA = {
    "type": "object",
    "properties": {"title_th": {"type": "string"}, "ad_th": {"type": "string"}, "title_en": {"type": "string"}, "ad_en": {"type": "string"}},
    "required": ["title_th", "ad_th", "title_en", "ad_en"],
    "additionalProperties": False,
}

AD_PROMPT = """You write job ads for a Thai block rubber (STR 20) factory with about 240 staff, many from Myanmar.
Write one short ad in Thai and one in English for the job below, for Facebook, LINE and job websites.

Rules:
- Plain, friendly words a factory worker understands. Short lines, a few bullet points.
- Say what the person will do, what we look for, the shift or working days, and how to apply.
- Do NOT mention pay, salary or a pay range. Write "Pay and benefits per company policy" instead.
- Do NOT ask for age, gender, religion, marital status or nationality unless the law requires it for this job.
  You may say "Thai or foreign workers with a valid work permit are welcome" for operator jobs.
- No promises the company has not made.

Job: {title} ({group}), unit: {unit}, number of people: {n}
Purpose: {purpose}
Main duties: {duties}
Education: {education}
Experience: {experience}
Extra from the Department Head: {extra}
Working time: {shift}
Benefits the company lists: {benefits}
How to apply: {apply}"""


def draft_ad(db: Session, req: Requisition) -> Optional[str]:
    from .ai.providers import AIError, get_provider

    job = req.job
    unit = db.get(OrgUnit, req.org_unit_code)
    prompt = AD_PROMPT.format(
        title=job.title_en, group=req.employee_group, unit=unit.name_en if unit else req.org_unit_code, n=req.headcount,
        purpose=job.purpose or "-", duties=(job.responsibilities or "-").replace("\n", "; "), education=job.education or "-",
        experience=job.experience or "-", extra=req.requirements or "-",
        shift="Factory calendar, rest day Friday, day and night shifts" if (unit and unit.calendar_code == "FACTORY") else "Office calendar, rest day Sunday",
        benefits=get_setting(db, "recruit_benefits") or "-", apply=get_setting(db, "recruit_apply_how") or "-",
    )
    try:
        res = get_provider().classify(prompt, AD_SCHEMA, {"task": "ad", "title": job.title_en, "title_th": job.title_th, "unit": unit.name_en if unit else "",
                                                          "n": req.headcount, "group": req.employee_group})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    d = res.data
    req.ad_th = f"{d.get('title_th', '')}\n\n{d.get('ad_th', '')}".strip()[:4000]
    req.ad_en = f"{d.get('title_en', '')}\n\n{d.get('ad_en', '')}".strip()[:4000]
    req.ad_ai = True
    return None


# ---------- candidates ----------

CV_FIELDS = ("full_name_en", "full_name_th", "nickname", "title", "gender", "nationality", "birth_date", "phone", "email", "line_id",
             "id_card_no", "home_area", "education", "experience_years", "experience", "skills")


def _f(kind: str = "string") -> dict:
    return {"type": "object", "properties": {"value": {"type": [kind, "null"]}, "confidence": {"type": "number"}},
            "required": ["value", "confidence"], "additionalProperties": False}


CV_SCHEMA = {
    "type": "object",
    "properties": {**{k: _f("integer" if k == "experience_years" else "string") for k in CV_FIELDS},
                   "page_quality": {"type": "string", "enum": ["good", "blurry", "cut_off", "not_a_cv"]}},
    "required": [*CV_FIELDS, "page_quality"],
    "additionalProperties": False,
}

CV_PROMPT = """You are reading a CV or a filled-in paper job application form (ใบสมัครงาน) for a Thai factory.
It may be in Thai, English or Myanmar, typed or handwritten. Return each field with your confidence from 0 to 1:
- full_name_en: name in Latin letters (transliterate Thai or Myanmar names if no Latin spelling is given; lower the confidence).
- full_name_th: name in Thai script if written. nickname if written.
- title: Mr., Ms. or Mrs. gender: M or F only if the form states it or the title shows it.
- nationality: Thai, Myanmar, Japanese or Other.
- birth_date as YYYY-MM-DD. Convert Thai Buddhist years (e.g. 2545) to Gregorian (2002).
- phone (digits only), email, line_id.
- id_card_no: Thai ID (13 digits) or passport / pink card number, digits and letters only.
- home_area: district and province only, never the full address.
- education: highest level and field, e.g. "Vocational certificate (ปวช.), mechanics".
- experience_years: whole years of work in total. experience: last jobs, one line each, newest first.
- skills: licences, machines, languages, computer skills, comma separated.
Never guess: return null with low confidence if a value is missing or unreadable.
Do not return religion, marital status, health, photos or family details even if written."""


def clean_phone(v: Optional[str]) -> Optional[str]:
    d = re.sub(r"\D", "", v or "")
    if d.startswith("66") and len(d) == 11:
        d = "0" + d[2:]
    return d or None


def clean_id(v: Optional[str]) -> Optional[str]:
    s = re.sub(r"[^0-9A-Za-z]", "", v or "").upper()
    return s or None


def valid_thai_id(s: str) -> bool:
    if not re.fullmatch(r"\d{13}", s or ""):
        return False
    total = sum(int(s[i]) * (13 - i) for i in range(12))
    return (11 - total % 11) % 10 == int(s[12])


def candidate_checks(db: Session, data: dict, cand_id: Optional[int] = None) -> list[dict]:
    """Checks on the person (not the job): run when the candidate is saved."""
    out = []
    idc, phone = data.get("id_card_no"), data.get("phone")
    if idc and data.get("nationality") == "Thai" and not valid_thai_id(idc):
        out.append(finding("flag", "cand_id_check"))
    if idc or phone:
        conds = []
        if idc:
            conds.append(Candidate.id_card_no == idc)
        if phone:
            conds.append(Candidate.phone == phone)
        for c in db.scalars(select(Candidate).where(or_(*conds), Candidate.erased_at.is_(None))):
            if c.id != cand_id:
                out.append(finding("flag", "cand_duplicate", ref=c.ref, name=c.full_name_en))
    emp = former_employee(db, data)
    if emp is not None:
        if emp.status == "Active":
            out.append(finding("block", "cand_active_employee", emp=emp.employee_id))
        elif emp.exit_reason_code in REHIRE_BLOCK:
            out.append(finding("block", "cand_rehire_blocked", emp=emp.employee_id, reason=emp.exit_reason_code, left=str(emp.exit_date)))
        elif emp.exit_reason_code in REHIRE_CHECK:
            out.append(finding("flag", "cand_rehire_check", emp=emp.employee_id, reason=emp.exit_reason_code, left=str(emp.exit_date)))
        else:
            out.append(finding("info", "cand_former", emp=emp.employee_id, reason=emp.exit_reason_code or "-", left=str(emp.exit_date or "")))
    return out


def former_employee(db: Session, data: dict) -> Optional[Employee]:
    if data.get("previous_employee_id"):
        e = db.get(Employee, data["previous_employee_id"])
        if e:
            return e
    idc = data.get("id_card_no")
    if idc:
        r = db.scalar(select(EmployeeRestricted).where(or_(EmployeeRestricted.id_card_no == idc, EmployeeRestricted.passport_no == idc)))
        if r:
            return r.employee
    name, bd = (data.get("full_name_en") or "").strip().lower(), data.get("birth_date")
    if name and bd:
        for e in db.scalars(select(Employee).where(Employee.birth_date == bd)):
            if (e.full_name_en or "").strip().lower() == name:
                return e
    return None


def clean_candidate(data: dict) -> dict:
    from .people import parse_date

    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            v = ("\n".join(" ".join(ln.split()) for ln in v.strip().splitlines()) if k in MULTILINE else " ".join(v.split())) or None
        out[k] = v
    if "phone" in out:
        out["phone"] = clean_phone(out["phone"])
    if "id_card_no" in out:
        out["id_card_no"] = clean_id(out["id_card_no"])
    if out.get("email"):
        out["email"] = out["email"].lower()
    if "birth_date" in out:
        out["birth_date"] = parse_date(out["birth_date"])
    if out.get("experience_years") not in (None, ""):
        try:
            out["experience_years"] = max(0, min(60, int(out["experience_years"])))
        except (TypeError, ValueError):
            out["experience_years"] = None
    elif "experience_years" in out:
        out["experience_years"] = None
    if "nationality" in out and out["nationality"] not in NATIONALITIES:
        out["nationality"] = "Thai" if not out["nationality"] else "Other"
    if "source" in out and out["source"] not in SOURCES:
        out["source"] = "walk_in"
    if "gender" in out and out["gender"] not in ("M", "F"):
        out["gender"] = None
    return out


MULTILINE = ("experience", "skills", "note")
EDITABLE = (*CV_FIELDS, "source", "referred_by", "previous_employee_id", "note")


def retention_date(db: Session, talent_pool: bool, today: Optional[date] = None) -> date:
    months = int(get_setting(db, "recruit_pool_months" if talent_pool else "recruit_retention_months") or 12)
    from .skills import add_months

    return add_months(today or date.today(), months)


def save_candidate(db: Session, user: User, data: dict, cand: Optional[Candidate] = None, consent: Optional[str] = None,
                   talent_pool: Optional[bool] = None) -> Candidate:
    data = clean_candidate(data)
    if not (data.get("full_name_en") or (cand is not None and "full_name_en" not in data and cand.full_name_en)):
        raise RecruitError("Name (EN) is required")
    if data.get("referred_by") and not db.get(Employee, data["referred_by"].upper()):
        raise RecruitError("Referred by: employee not found")
    if data.get("previous_employee_id") and not db.get(Employee, data["previous_employee_id"].upper()):
        raise RecruitError("Former Employee ID not found")
    new = cand is None
    if new:
        cand = Candidate(ref=next_ref(db, "CA", date.today().year), created_by=user.label, is_sample="(SAMPLE)" in (data.get("full_name_en") or ""))
        db.add(cand)
    changed = []
    for k in EDITABLE:
        if k in data and getattr(cand, k) != data[k]:
            if k in ("referred_by", "previous_employee_id") and data[k]:
                data[k] = data[k].upper()
            setattr(cand, k, data[k])
            changed.append(k)
    if former_employee(db, data) and not cand.previous_employee_id:
        cand.previous_employee_id = former_employee(db, data).employee_id
        cand.source = cand.source if cand.source != "walk_in" else "rehire"
    if consent and not cand.consent_at:
        cand.consent_at, cand.consent_how = datetime.now(), consent
    if talent_pool is not None:
        cand.talent_pool = talent_pool
    cand.retain_until = retention_date(db, cand.talent_pool)
    db.flush()
    audit(db, user, "create" if new else "update", "candidate", cand.ref, ", ".join(changed)[:300] if not new else cand.full_name_en)
    return cand


def store_cv(data: bytes, mime: str, original: str) -> tuple[str, str]:
    if mime not in CV_MIME:
        raise RecruitError("Please upload a photo (JPG, PNG) or a PDF")
    if len(data) > MAX_FILE:
        raise RecruitError("The file is over 10 MB")
    if mime != "application/pdf":
        from .ai.capture import store_file

        rel, mime = store_file(data, mime, original)
        return rel, mime
    now = datetime.now()
    rel = f"cv/{now:%Y/%m}/{uuid.uuid4().hex}.pdf"
    path = settings.storage_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel, mime


def read_cv(db: Session, user: User, data: bytes, mime: str, original: str, source: str = "walk_in") -> Candidate:
    """Store the CV or application photo, let AI read it and create the candidate for HR to check."""
    from .ai.providers import AIError, get_provider

    rel, mime = store_cv(data, mime, original)
    try:
        res = get_provider().extract((settings.storage_dir / rel).read_bytes(), mime, CV_SCHEMA, CV_PROMPT, {"form_type": "CV", "name": original})
    except AIError as e:
        raise RecruitError(str(e)) from e
    except Exception as e:  # network, bad JSON
        raise RecruitError(f"{type(e).__name__}: {e}"[:300]) from e
    d = res.data or {}
    if d.get("page_quality") == "not_a_cv":
        raise RecruitError("This does not look like a CV or an application form")
    values = {k: (d.get(k) or {}).get("value") for k in CV_FIELDS}
    conf = {k: round(float((d.get(k) or {}).get("confidence") or 0), 2) for k in CV_FIELDS if (d.get(k) or {}).get("value") not in (None, "")}
    values["source"] = source
    if not values.get("full_name_en"):
        values["full_name_en"] = values.get("full_name_th") or "Name not read"
        conf["full_name_en"] = 0
    cand = save_candidate(db, user, values)
    cand.file_path, cand.file_mime, cand.ai_used, cand.ai_confidence = rel, mime, True, conf
    audit(db, user, "ai_read", "candidate", cand.ref, f"{res.provider} read {original[:80]}")
    return cand


def low_confidence(cand: Candidate) -> set[str]:
    thr = settings.ai_review_threshold
    return {k for k, v in (cand.ai_confidence or {}).items() if v < thr}


def erase(db: Session, user: User, cand: Candidate) -> None:
    """PDPA: remove personal data after the retention period. Stage history stays for the numbers."""
    if any(a.stage in ACTIVE_STAGES for a in cand.applications):
        raise RecruitError("The candidate is still in an open application")
    if any(a.stage == "hired" for a in cand.applications):
        raise RecruitError("Hired: the data now lives in the employee record")
    if cand.file_path:
        p = (settings.storage_dir / cand.file_path).resolve()
        if settings.storage_dir.resolve() in p.parents and p.exists():
            p.unlink()
    for k in CV_FIELDS + ("referred_by", "note", "file_path", "file_mime", "previous_employee_id"):
        if k != "nationality":
            setattr(cand, k, None)
    cand.full_name_en = f"Erased {cand.ref}"
    cand.ai_confidence, cand.erased_at, cand.talent_pool = None, datetime.now(), False
    audit(db, user, "erase", "candidate", cand.ref, "Personal data erased (retention period ended)")


# ---------- applications ----------

def screen(db: Session, cand: Candidate, req: Requisition, today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    out = []
    if not cand.consent_at:
        out.append(finding("block", "cand_no_consent"))
    if req.status != "open":
        out.append(finding("block", "app_req_closed", ref=req.ref, status=req.status))
    age = cand.age_on(today)
    juvenile = int(get_setting(db, "juvenile_age") or 18)
    if age is None:
        out.append(finding("flag", "cand_no_birth"))
    elif age < 15:
        out.append(finding("block", "cand_under_15", age=age))
    elif age < juvenile:
        out.append(finding("flag", "cand_juvenile", age=age))
    if cand.nationality not in ("Thai", "Japanese"):
        out.append(finding("flag", "cand_permit"))
    out += [f for f in candidate_checks(db, {k: getattr(cand, k) for k in ("id_card_no", "phone", "nationality", "full_name_en", "birth_date", "previous_employee_id")}, cand.id)]
    others = [a.requisition.ref for a in cand.applications if a.requisition_id != req.id and a.stage in ACTIVE_STAGES]
    if others:
        out.append(finding("info", "app_other_open", refs=", ".join(others)))
    return out


def apply(db: Session, user: User, cand: Candidate, req: Requisition, note: str = "") -> Application:
    if cand.erased_at:
        raise RecruitError("Candidate data was erased")
    if db.scalar(select(Application).where(Application.requisition_id == req.id, Application.candidate_id == cand.id)):
        raise RecruitError(f"{cand.ref} is already on {req.ref}")
    if req.status != "open":
        raise RecruitError(f"{req.ref} is not open")
    app = Application(requisition_id=req.id, candidate_id=cand.id, stage="applied", stage_at=datetime.now(), applied_on=date.today(),
                      history=[{"stage": "applied", "at": datetime.now().isoformat(timespec="minutes"), "by": user.label, "note": note[:300]}])
    app.requisition, app.candidate = req, cand
    db.add(app)
    db.flush()
    app.checks = screen(db, cand, req)
    cand.retain_until = max(cand.retain_until or date.today(), retention_date(db, cand.talent_pool))
    audit(db, user, "apply", "application", app.id, f"{cand.ref} -> {req.ref}")
    return app


def blocks(app: Application) -> list[dict]:
    return [c for c in app.checks or [] if c["level"] == "block"]


ORDER = ["applied", "screen", "interview", "offer", "hired"]


def move(db: Session, user: User, app: Application, stage: str, note: str = "", reason: str = "", force: bool = False) -> None:
    if stage not in STAGES:
        raise RecruitError("Unknown stage")
    if app.stage in ("hired", "rejected", "withdrawn") and not force:
        if not (app.stage in ("rejected", "withdrawn") and stage in ACTIVE_STAGES and user.has(*HR)):
            raise RecruitError("This application is closed")
    if stage == "hired":
        raise RecruitError("Use Hire: the new employee goes through the normal approval")
    if stage == "rejected" and reason not in REJECT_REASONS:
        raise RecruitError("Choose why the candidate was not selected")
    if stage == "withdrawn" and reason not in WITHDRAW_REASONS:
        raise RecruitError("Choose why the candidate withdrew")
    if stage in ("interview", "offer") and blocks(app) and not force:
        raise RecruitError("Fix the blocking checks first: " + "; ".join(rule_en(c) for c in blocks(app)))
    if stage == "offer":
        raise RecruitError("Use Make offer")
    prev = app.stage
    app.stage, app.stage_at = stage, datetime.now()
    if stage in ("rejected", "withdrawn"):
        app.close_reason, app.close_note = reason, note[:300] or None
        if app.offer_status == "pending":
            app.offer_status = "declined" if stage == "withdrawn" else "withdrawn"
    app.history = [*(app.history or []), {"stage": stage, "at": datetime.now().isoformat(timespec="minutes"), "by": user.label,
                                          "note": (f"{REJECT_REASONS.get(reason) or WITHDRAW_REASONS.get(reason) or ''} {note}").strip()[:300]}]
    audit(db, user, "move", "application", app.id, f"{prev} -> {stage}")


def recheck(db: Session, app: Application) -> None:
    app.checks = screen(db, app.candidate, app.requisition)


# ---------- interviews ----------

INTERVIEW_Q = {
    "TH1": ("Tell me about a problem at work that kept coming back. What did you do to find the cause?",
            "เล่าปัญหาในงานที่เกิดซ้ำ ๆ คุณทำอย่างไรเพื่อหาสาเหตุ"),
    "TH2": ("Tell me about a busy day when many things had to be done. How did you plan them?",
            "เล่าวันที่งานยุ่งและมีหลายอย่างต้องทำ คุณวางแผนอย่างไร"),
    "TH3": ("What is the last new machine, tool or task you learned? How did you learn it and how long did it take?",
            "เครื่องจักร เครื่องมือ หรืองานใหม่ล่าสุดที่คุณเรียนรู้คืออะไร เรียนอย่างไร ใช้เวลานานแค่ไหน"),
    "TH4": ("Tell me about something you changed to make the work easier, safer or faster.",
            "เล่าสิ่งที่คุณเคยเปลี่ยนเพื่อให้งานง่ายขึ้น ปลอดภัยขึ้น หรือเร็วขึ้น"),
    "PE1": ("How do you hand over work to the next shift or person? Show me what you would say.",
            "คุณส่งต่องานให้กะถัดไปหรือคนถัดไปอย่างไร ลองพูดให้ฟังว่าจะพูดอะไร"),
    "PE2": ("Tell me about a time a colleague was behind or had a problem. What did you do?",
            "เล่าครั้งที่เพื่อนร่วมงานทำงานไม่ทันหรือมีปัญหา คุณทำอะไร"),
    "PE3": ("Tell me about a time you needed help from another department. How did you get it?",
            "เล่าครั้งที่คุณต้องการความช่วยเหลือจากแผนกอื่น คุณทำอย่างไรจึงได้รับ"),
    "PE4": ("Who uses your work after you finish it? How do you make sure it is right for them?",
            "ใครเป็นผู้ใช้งานต่อจากคุณ คุณทำอย่างไรให้งานถูกต้องสำหรับเขา"),
    "SE1": ("In your last job, how often were you late or absent? What do you do when you cannot come to work?",
            "ในงานล่าสุด คุณมาสายหรือขาดงานบ่อยแค่ไหน ถ้ามาทำงานไม่ได้คุณทำอย่างไร"),
    "SE2": ("Tell me about the most stressful day at work you remember. What did you do?",
            "เล่าวันที่เครียดที่สุดในการทำงานที่คุณจำได้ คุณทำอย่างไร"),
    "SE3": ("Tell me about a mistake you made at work. What did you do after you found it?",
            "เล่าความผิดพลาดที่คุณเคยทำในงาน หลังจากรู้ตัวแล้วคุณทำอะไร"),
    "SE4": ("What would you like to be able to do in two years? What are you doing now to get there?",
            "อีกสองปีคุณอยากทำอะไรได้ ตอนนี้คุณทำอะไรเพื่อไปถึงตรงนั้น"),
    "DE1": ("How do you check that your work meets the standard before you pass it on?",
            "คุณตรวจอย่างไรว่างานได้มาตรฐานก่อนส่งต่อ"),
    "DE2": ("Tell me about a time you saw something unsafe. What did you do?",
            "เล่าครั้งที่คุณเห็นสิ่งที่ไม่ปลอดภัย คุณทำอะไร"),
    "DE3": ("Tell me about a target you had to reach. Did you reach it? What did you do when it was hard?",
            "เล่าเป้าหมายที่คุณต้องทำให้ได้ ทำได้ไหม เมื่อยากคุณทำอย่างไร"),
    "DE4": ("Give an example of how you saved material, energy or time at work.",
            "ยกตัวอย่างที่คุณประหยัดวัตถุดิบ พลังงาน หรือเวลาในงาน"),
    "LE1": ("Tell me about a person you trained. How did you know they had learned it?",
            "เล่าถึงคนที่คุณเคยสอนงาน คุณรู้ได้อย่างไรว่าเขาทำได้แล้ว"),
    "LE2": ("Tell me about a time your team missed the plan. What did you do as the leader?",
            "เล่าครั้งที่ทีมทำไม่ได้ตามแผน คุณในฐานะหัวหน้าทำอะไร"),
    "LE3": ("Tell me about a hard decision you made without all the facts. How did you decide?",
            "เล่าการตัดสินใจยากที่คุณทำโดยไม่มีข้อมูลครบ คุณตัดสินใจอย่างไร"),
    "LE4": ("Tell me about a change you led that people did not like at first. What did you do?",
            "เล่าการเปลี่ยนแปลงที่คุณนำ และตอนแรกคนไม่ชอบ คุณทำอย่างไร"),
}
CORE = ["SE1", "DE2", "DE1", "PE2", "TH3"]
STAFF_EXTRA = ["PE1", "TH1", "TH2"]
LEADER_EXTRA = ["LE2", "LE1", "LE3"]


def criteria(db: Session, req: Requisition) -> list[dict]:
    """What the interview scores: the job's most important skills plus Growth Compass competencies."""
    from .growth import LEADER_GRADES, LEADER_JOBS, competencies

    comps = competencies(db)
    codes = list(CORE)
    if req.employee_group != "Operator":
        codes += STAFF_EXTRA
    if req.job_code in LEADER_JOBS or (req.grade_code or "") in LEADER_GRADES or req.employee_group == "Manager":
        codes += LEADER_EXTRA
    out = []
    js = db.scalars(select(JobSkill).where(JobSkill.job_code == req.job_code).order_by(JobSkill.level.desc())).all()
    for j in js[:3]:
        out.append({"code": j.skill_code, "kind": "skill", "en": j.skill.name_en, "th": j.skill.name_th or j.skill.name_en,
                    "q_en": f"Show or explain how you would do this: {j.skill.name_en}. What could go wrong?",
                    "q_th": f"แสดงหรืออธิบายวิธีทำงานนี้: {j.skill.name_th or j.skill.name_en} อะไรที่อาจผิดพลาดได้"})
    for c in codes:
        if c in comps:
            q = INTERVIEW_Q.get(c, ("", ""))
            out.append({"code": c, "kind": "competency", "en": comps[c].name_en, "th": comps[c].name_th or comps[c].name_en, "q_en": q[0], "q_th": q[1]})
    return out


def schedule(db: Session, user: User, app: Application, when: datetime, kind: str, interviewer_id: Optional[str]) -> Interview:
    if app.stage not in ("applied", "screen", "interview"):
        raise RecruitError("Interviews are planned before the offer")
    if blocks(app):
        raise RecruitError("Fix the blocking checks first: " + "; ".join(rule_en(c) for c in blocks(app)))
    who = db.get(Employee, interviewer_id) if interviewer_id else None
    if interviewer_id and who is None:
        raise RecruitError("Interviewer not found")
    iv = Interview(application_id=app.id, kind=kind, when=when, interviewer_employee_id=who.employee_id if who else None,
                   interviewer=who.full_name_en if who else user.label,
                   questions=[{"criterion": c["code"], "text": c["q_en"], "text_th": c["q_th"]} for c in criteria(db, app.requisition)])
    iv.application = app
    db.add(iv)
    if app.stage != "interview":
        move(db, user, app, "interview", f"{kind} on {when:%d/%m %H:%M}")
    db.flush()
    audit(db, user, "create", "interview", iv.id, f"{app.candidate.ref} {when:%Y-%m-%d %H:%M}")
    return iv


Q_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "criterion": {"type": "string"}, "text": {"type": "string"}, "text_th": {"type": "string"}, "listen_for": {"type": "string"}},
        "required": ["criterion", "text", "text_th", "listen_for"], "additionalProperties": False}}},
    "required": ["questions"],
    "additionalProperties": False,
}

Q_PROMPT = """You help a supervisor at a Thai block rubber factory interview a candidate.
Write one behaviour question for each criterion below ("Tell me about a time..." or "Show me how..."),
in English and Thai, and one short line on what a good answer contains (listen_for).
Use the candidate's background so the questions fit their experience. Keep the language simple.
Never ask about age, marriage, pregnancy, religion, health, union membership or family plans.

Job: {job} in {unit}
Candidate background: {background}
Criteria (code: name):
{criteria}"""


def suggest_questions(db: Session, iv: Interview) -> Optional[str]:
    from .ai.providers import AIError, get_provider

    app = iv.application
    req, cand = app.requisition, app.candidate
    crit = criteria(db, req)
    prompt = Q_PROMPT.format(job=req.job.title_en, unit=req.org_unit.name_en,
                             background=f"{cand.education or '-'}; {cand.experience_years or 0} years; {cand.experience or '-'}; skills: {cand.skills or '-'}",
                             criteria="\n".join(f"- {c['code']}: {c['en']}" for c in crit))
    try:
        res = get_provider().classify(prompt, Q_SCHEMA, {"task": "interview_q", "criteria": crit})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    codes = {c["code"] for c in crit}
    qs = [{"criterion": q["criterion"], "text": q["text"][:400], "text_th": q.get("text_th", "")[:400], "listen_for": q.get("listen_for", "")[:300]}
          for q in res.data.get("questions") or [] if q.get("criterion") in codes]
    if not qs:
        return "The AI answer had no usable questions"
    iv.questions, iv.ai_questions = qs, True
    return None


def can_score(db: Session, user: User, iv: Interview) -> bool:
    if user.has(*HR):
        return True
    if iv.interviewer_employee_id and iv.interviewer_employee_id == user.employee_id:
        return True
    return False


def score(db: Session, user: User, iv: Interview, scores: dict, evidence: dict, rec: str, note: str) -> None:
    if rec not in RECOMMEND:
        raise RecruitError("Choose a recommendation")
    crit = [q["criterion"] for q in iv.questions or []]
    clean: dict[str, int] = {}
    for c in crit:
        try:
            v = int(scores.get(c) or 0)
        except (TypeError, ValueError):
            v = 0
        if 1 <= v <= 5:
            clean[c] = v
    if len(clean) < max(1, len(crit) // 2):
        raise RecruitError("Score at least half of the criteria (leave a line empty only if it did not come up)")
    missing = [c for c, v in clean.items() if v in (1, 5) and not (evidence.get(c) or "").strip()]
    if missing:
        raise RecruitError("Write what you saw or heard for every 1 or 5: " + ", ".join(missing))
    iv.scores = clean
    iv.evidence = {c: (evidence.get(c) or "").strip()[:500] for c in crit if (evidence.get(c) or "").strip()}
    iv.recommendation, iv.note, iv.done_at = rec, (note or "")[:2000] or None, datetime.now()
    audit(db, user, "score", "interview", iv.id, f"{iv.application.candidate.ref}: {rec}")


def interview_summary(app: Application) -> dict:
    done = [i for i in app.interviews if i.done_at]
    if not done:
        return {"n": 0}
    by: dict[str, list[int]] = defaultdict(list)
    for i in done:
        for c, v in (i.scores or {}).items():
            by[c].append(v)
    avg = {c: round(sum(v) / len(v), 1) for c, v in by.items()}
    return {"n": len(done), "avg": avg, "recs": Counter(i.recommendation for i in done),
            "mean": round(sum(avg.values()) / len(avg), 2) if avg else None}


# ---------- offer and hire ----------

def offer(db: Session, user: User, app: Application, start: date, note: str = "") -> None:
    if app.stage not in ("interview", "screen"):
        raise RecruitError("Offer after the interview")
    recheck(db, app)
    if blocks(app):
        raise RecruitError("Fix the blocking checks first: " + "; ".join(rule_en(c) for c in blocks(app)))
    s = interview_summary(app)
    if not s["n"]:
        raise RecruitError("Record at least one interview scorecard first")
    if start < date.today():
        raise RecruitError("Start date is in the past")
    if app.requisition.open_seats <= 0:
        raise RecruitError("All seats on this request are filled")
    app.start_date, app.offer_note, app.offer_status, app.offer_at = start, note[:300] or None, "pending", datetime.now()
    app.stage, app.stage_at = "offer", datetime.now()
    app.history = [*(app.history or []), {"stage": "offer", "at": datetime.now().isoformat(timespec="minutes"), "by": user.label,
                                          "note": f"Start {start:%d/%m/%Y}. {note}"[:300]}]
    audit(db, user, "offer", "application", app.id, f"{app.candidate.ref} start {start}")


def offer_answer(db: Session, user: User, app: Application, accepted: bool, reason: str = "", note: str = "") -> None:
    if app.stage != "offer" or app.offer_status != "pending":
        raise RecruitError("No open offer")
    if accepted:
        app.offer_status = "accepted"
        app.history = [*(app.history or []), {"stage": "offer", "at": datetime.now().isoformat(timespec="minutes"), "by": user.label, "note": "Offer accepted"}]
        audit(db, user, "offer_accept", "application", app.id, app.candidate.ref)
    else:
        move(db, user, app, "withdrawn", note or "Offer declined", reason or "other")
        app.offer_status = "declined"


def hire_data(db: Session, app: Application) -> dict:
    """What goes into the new-employee request. HR checks it on the hire form."""
    c, r = app.candidate, app.requisition
    group = r.employee_group
    start = app.start_date or date.today()
    return {
        "employee_id": next_free_employee_id(db, group), "employee_group": group, "pay_type": "Daily" if group == "Operator" else "Monthly",
        "title": c.title, "gender": c.gender, "full_name_en": c.full_name_en, "full_name_th": c.full_name_th, "nickname": c.nickname,
        "nationality": c.nationality, "birth_date": c.birth_date, "join_date": start, "probation_end_date": start + timedelta(days=118),
        "org_unit_code": r.org_unit_code, "position_id": r.position_id, "job_code": r.job_code, "grade_code": r.grade_code,
        "previous_employee_id": c.previous_employee_id,
    }


def hire(db: Session, user: User, app: Application, data: dict) -> ApprovalRequest:
    from .people import request_create

    if app.stage != "offer" or app.offer_status != "accepted":
        raise RecruitError("Hire after the candidate accepts the offer")
    if app.hire_approval_id:
        appr = db.get(ApprovalRequest, app.hire_approval_id)
        if appr and appr.status == "pending":
            raise RecruitError("The new-employee request is already waiting for approval")
    c = app.candidate
    restricted = {}
    if c.id_card_no:
        restricted["id_card_no" if c.nationality == "Thai" else "passport_no"] = c.id_card_no
    appr = request_create(db, user, data, restricted)
    appr.payload = {**(appr.payload or {}), "application_id": app.id}
    app.hire_approval_id = appr.id
    app.history = [*(app.history or []), {"stage": "offer", "at": datetime.now().isoformat(timespec="minutes"), "by": user.label,
                                          "note": f"New-employee request {data.get('employee_id')} sent for approval"}]
    audit(db, user, "hire", "application", app.id, f"{c.ref} -> {data.get('employee_id')}")
    return appr


def on_hired(db: Session, appr: ApprovalRequest, emp: Employee, user: User) -> None:
    """Called when the new-employee request from a hire is approved."""
    app = db.get(Application, (appr.payload or {}).get("application_id"))
    if app is None:
        return
    app.stage, app.stage_at, app.employee_id = "hired", datetime.now(), emp.employee_id
    app.history = [*(app.history or []), {"stage": "hired", "at": datetime.now().isoformat(timespec="minutes"), "by": user.label, "note": emp.employee_id}]
    req = app.requisition
    req.hired = (req.hired or 0) + 1
    if req.hired >= req.headcount:
        req.status, req.closed_on = "filled", date.today()
    if app.candidate.referred_by:
        audit(db, user, "referral", "employee", app.candidate.referred_by, f"Referred {emp.employee_id} (hired)")


def on_hire_rejected(db: Session, appr: ApprovalRequest) -> None:
    app = db.get(Application, (appr.payload or {}).get("application_id"))
    if app is not None and app.hire_approval_id == appr.id:
        app.hire_approval_id = None


# ---------- access ----------

def can_see_req(db: Session, user: User, req: Requisition) -> bool:
    scope = user_scope(db, user)
    return scope.all or req.org_unit_code in scope.org_units or any(
        i.interviewer_employee_id == user.employee_id for a in req.applications for i in a.interviews if user.employee_id)


def visible_reqs(db: Session, user: User) -> list[Requisition]:
    scope = user_scope(db, user)
    q = select(Requisition).order_by(Requisition.id.desc())
    if not scope.all:
        q = q.where(Requisition.org_unit_code.in_(scope.org_units or {"-"}))
    return list(db.scalars(q))


# ---------- numbers ----------

@dataclass
class Stats:
    open_reqs: int
    open_seats: int
    pending_reqs: int
    late_reqs: int
    active_apps: int
    hires: int
    avg_days_to_fill: Optional[float]
    offer_accept: Optional[float]
    funnel: list[tuple[str, int]]
    sources: list[tuple[str, int, int]]  # source, applicants, hires
    not_selected: list[tuple[str, int]]
    withdrew: list[tuple[str, int]]
    by_month: list[tuple[str, int]]


def stats(db: Session, reqs: list[Requisition], today: Optional[date] = None, since: Optional[date] = None) -> Stats:
    today = today or date.today()
    since = since or today - timedelta(days=365)
    apps = [a for r in reqs for a in r.applications if a.applied_on >= since]
    reached = Counter()
    for a in apps:
        seen = {h["stage"] for h in a.history or []} | {a.stage}
        for s in ORDER:
            if s in seen:
                reached[s] += 1
    hired = [a for a in apps if a.stage == "hired"]
    fill_days = []
    for a in hired:
        r = a.requisition
        when = next((datetime.fromisoformat(h["at"]).date() for h in a.history or [] if h["stage"] == "hired"), None)
        if r.opened_on and when:
            fill_days.append((when - r.opened_on).days)
    offers = [a for a in apps if a.offer_status in ("accepted", "declined")]
    src: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for a in apps:
        src[a.candidate.source][0] += 1
        if a.stage == "hired":
            src[a.candidate.source][1] += 1
    months = Counter(a.applied_on.strftime("%Y-%m") for a in apps)
    first = date(today.year, today.month, 1)
    keys = []
    for i in range(5, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        keys.append(f"{y}-{m:02d}")
    open_r = [r for r in reqs if r.status == "open"]
    return Stats(
        open_reqs=len(open_r), open_seats=sum(r.open_seats for r in open_r), pending_reqs=sum(1 for r in reqs if r.status == "pending"),
        late_reqs=sum(1 for r in open_r if r.needed_by < today), active_apps=sum(1 for a in apps if a.stage in ACTIVE_STAGES), hires=len(hired),
        avg_days_to_fill=round(sum(fill_days) / len(fill_days), 1) if fill_days else None,
        offer_accept=round(100 * sum(1 for a in offers if a.offer_status == "accepted") / len(offers)) if offers else None,
        funnel=[(s, reached[s]) for s in ORDER],
        sources=sorted(((k, v[0], v[1]) for k, v in src.items()), key=lambda x: -x[1]),
        not_selected=Counter(a.close_reason for a in apps if a.stage == "rejected").most_common(),
        withdrew=Counter(a.close_reason for a in apps if a.stage == "withdrawn").most_common(),
        by_month=[(k, months.get(k, 0)) for k in keys],
    )


def rule_en(f: dict) -> str:
    from ..i18n import rule_text

    return rule_text("en", f)
