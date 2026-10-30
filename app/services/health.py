"""Data Health: rules that flag records for an owner with a due date.
Run nightly and on demand. Issues close themselves once the data is fixed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DailyAttendance,
    DataIssue,
    Employee,
    ExitCase,
    LeaveRequest,
    OrgUnit,
    OTRequest,
    Position,
    ScanImport,
)
from .common import get_setting, valid_employee_id

RULES = {
    "ID_FORMAT": ("block", "hr", "Employee ID format"),
    "REQUIRED": ("block", "hr", "Required fields missing"),
    "ORG_HEAD": ("block", "hr", "Org Unit has no Department Head"),
    "NO_POSITION": ("flag", "hr", "Active employee has no Position"),
    "POS_OVER": ("flag", "hr", "Position has more people than seats"),
    "PERMIT_MISSING": ("flag", "hr", "Foreign worker with no work permit data"),
    "PERMIT_EXPIRY": ("flag", "hr", "Work permit expires soon"),
    "JUVENILE": ("info", "dept_head", "Juvenile worker (under 18)"),
    "PROBATION": ("flag", "dept_head", "Probation ends soon"),
    "CONTRACT_END": ("flag", "hr", "Contract ends soon"),
    "STATUS_EXIT": ("block", "hr", "Status and exit date disagree"),
    "EXIT_REASON": ("flag", "hr", "Exit with no reason code"),
    "WORK_AFTER_EXIT": ("block", "hr", "Attendance after exit date"),
    "OT_NO_REQUEST": ("flag", "dept_head", "Scan OT with no approved request"),
    "OT_SCAN_SHORT": ("flag", "dept_head", "OT not covered by face scan"),
    "SCAN_UNKNOWN": ("flag", "hr", "Face scan user not in the master"),
    "NO_EMAIL": ("info", "hr", "Manager or staff with no work email (cannot sign in)"),
    "EXIT_CLEARANCE": ("flag", "hr", "Exit clearance still open a week after the last day"),
    "SSO_DUE": ("block", "hr", "Social security not told of an exit by the deadline"),
    "LEAVE_SICK_DOC": ("flag", "dept_head", "Sick leave of 3+ days with no medical certificate"),
    "EXIT_NO_CASE": ("flag", "hr", "Exit with no clearance case"),
    "SKILL_COVERAGE": ("flag", "dept_head", "Critical skill has too few people at U/O"),
    "LICENCE_LAPSED": ("block", "dept_head", "Licence or safety refresher lapsed"),
    "ONBOARD_LATE": ("flag", "hr", "Onboarding item overdue"),
    "PROBATION_LATE": ("block", "dept_head", "Probation review not decided before the end date"),
}


@dataclass
class Hit:
    rule: str
    entity: str
    entity_id: str
    message: str
    employee_id: Optional[str] = None
    org_unit: Optional[str] = None
    due: Optional[date] = None


def _employees(db: Session, today: date) -> Iterator[Hit]:
    permit_days = int(get_setting(db, "work_permit_alert_days") or 60)
    prob_days = int(get_setting(db, "probation_alert_days") or 14)
    juvenile_age = int(get_setting(db, "juvenile_age") or 18)
    for e in db.scalars(select(Employee)):
        h = dict(entity="employee", entity_id=e.employee_id, employee_id=e.employee_id, org_unit=e.org_unit_code)
        if not valid_employee_id(e.employee_id, e.employee_group):
            yield Hit("ID_FORMAT", message=f"{e.employee_id} does not match the {e.employee_group} ID rule", **h)
        if e.status == "Resigned" and not e.exit_date:
            yield Hit("STATUS_EXIT", message="Resigned with no exit date", **h)
        if e.status == "Active" and e.exit_date and e.exit_date < today:
            yield Hit("STATUS_EXIT", message=f"Active but exit date {e.exit_date} has passed", **h)
        if e.status == "Resigned" and e.exit_date and not e.exit_reason_code and e.exit_date >= date(2026, 11, 1):
            yield Hit("EXIT_REASON", message="Exit has no reason code", **h)
        if e.status != "Active":
            continue
        missing = [f for f in ("birth_date", "nationality", "org_unit_code") if not getattr(e, f)]
        if missing:
            yield Hit("REQUIRED", message="Missing: " + ", ".join(missing), **h)
        if not e.position_id:
            yield Hit("NO_POSITION", message="No Position ID", **h)
        if e.is_foreign_worker:
            r = e.restricted
            if not r or not r.work_permit_no or not r.work_permit_expiry:
                yield Hit("PERMIT_MISSING", message="No work permit number or expiry", due=today + timedelta(days=30), **h)
            elif r.work_permit_expiry <= today + timedelta(days=permit_days):
                yield Hit("PERMIT_EXPIRY", message=f"Work permit expires {r.work_permit_expiry}", due=r.work_permit_expiry - timedelta(days=14), **h)
        age = e.age_on(today)
        if age is not None and age < juvenile_age:
            yield Hit("JUVENILE", message=f"Age {age}: OT, holiday and night work are blocked until {e.birth_date.replace(year=e.birth_date.year + juvenile_age)}", **h)
        if e.probation_end_date and today <= e.probation_end_date <= today + timedelta(days=prob_days + 16):
            yield Hit("PROBATION", message=f"Probation ends {e.probation_end_date}: evaluation due", due=e.probation_end_date - timedelta(days=prob_days), **h)
        if e.contract_end_date and today <= e.contract_end_date <= today + timedelta(days=60):
            yield Hit("CONTRACT_END", message=f"Contract ends {e.contract_end_date}", due=e.contract_end_date - timedelta(days=30), **h)
        if e.employee_group in ("Manager", "Staff") and not e.work_email:
            yield Hit("NO_EMAIL", message="No work email", **h)


def _org(db: Session, today: date) -> Iterator[Hit]:
    for u in db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True), OrgUnit.level == "department")):
        if not u.head_employee_id:
            yield Hit("ORG_HEAD", "org_unit", u.code, f"{u.name_en} has no Department Head", org_unit=u.code)
    for p in db.scalars(select(Position).where(Position.active.is_(True))):
        if p.filled > p.seats:
            yield Hit("POS_OVER", "position", p.position_id, f"{p.title}: {p.filled} people on {p.seats} seats", org_unit=p.org_unit_code)


def _time(db: Session, today: date) -> Iterator[Hit]:
    since = today - timedelta(days=45)
    for a in db.scalars(select(DailyAttendance).where(DailyAttendance.work_date >= since, DailyAttendance.scan_count > 0)):
        e = a.employee
        if e.exit_date and a.work_date > e.exit_date:
            yield Hit("WORK_AFTER_EXIT", "attendance", f"{e.employee_id}:{a.work_date}", f"Scans on {a.work_date} after exit {e.exit_date}", e.employee_id, e.org_unit_code)
    limit = int(get_setting(db, "unrequested_ot_min") or 30)
    reqs = {
        (r.employee_id, r.work_date)
        for r in db.scalars(select(OTRequest).where(OTRequest.work_date >= since, OTRequest.status.in_(("pending", "approved", "verified", "paid"))))
    }
    for a in db.scalars(select(DailyAttendance).where(DailyAttendance.work_date >= since, DailyAttendance.scan_ot_minutes > limit)):
        if (a.employee_id, a.work_date) not in reqs:
            yield Hit(
                "OT_NO_REQUEST", "attendance", f"{a.employee_id}:{a.work_date}",
                f"{a.work_date}: last scan {a.last_out:%H:%M}, {a.scan_ot_minutes / 60:.1f}h after shift end, no OT request",
                a.employee_id, a.employee.org_unit_code, a.work_date + timedelta(days=7),
            )
    for r in db.scalars(select(OTRequest).where(OTRequest.work_date >= since, OTRequest.status.in_(("approved", "verified")))):
        if any(f["code"] == "scan_short" for f in (r.flags or [])):
            yield Hit("OT_SCAN_SHORT", "ot_request", r.ref, f"{r.ref}: scans cover {(r.scan_minutes or 0) / 60:.1f}h of {r.requested_hours}h", r.employee_id, r.employee.org_unit_code)
    unknown = set()
    for imp in db.scalars(select(ScanImport).where(ScanImport.created_at >= datetime.combine(since, datetime.min.time()))):
        unknown |= {u for u in (imp.unknown_users or []) if not u.startswith("unreadable")}
    for u in sorted(unknown):
        yield Hit("SCAN_UNKNOWN", "scan_user", u, f"Scan user {u} is not in the employee master")


def _hrdev(db: Session, today: date) -> Iterator[Hit]:
    from .exits import sso_due

    open_cases = db.scalars(select(ExitCase).where(ExitCase.status == "open")).all()
    for c in open_cases:
        e = c.employee
        h = dict(employee_id=e.employee_id, org_unit=e.org_unit_code)
        left = c.open_items
        if left and today > c.last_day + timedelta(days=7):
            yield Hit("EXIT_CLEARANCE", "exit_case", c.ref, f"{c.ref}: {len(left)} item(s) open: " + ", ".join(i["code"] for i in left), due=c.last_day + timedelta(days=14), **h)
        due = sso_due(db, c.last_day)
        if any(i["code"] == "SSO" for i in left) and today >= due - timedelta(days=5):
            yield Hit("SSO_DUE", "exit_case", f"{c.ref}:SSO", f"{c.ref}: tell social security (SPS 6-09) by {due}", due=due, **h)
    has_case = set(db.scalars(select(ExitCase.employee_id)).all())
    for e in db.scalars(select(Employee).where(Employee.status == "Resigned", Employee.exit_date >= today - timedelta(days=60))):
        if e.employee_id not in has_case and e.exit_date >= date(2026, 11, 1):
            yield Hit("EXIT_NO_CASE", "employee", e.employee_id, f"Left {e.exit_date} with no clearance checklist", e.employee_id, e.org_unit_code)
    for r in db.scalars(select(LeaveRequest).where(LeaveRequest.leave_type == "SICK", LeaveRequest.status.in_(("pending", "approved")),
                                                   LeaveRequest.end_date >= today - timedelta(days=60), LeaveRequest.has_document.is_(False))):
        need = r.type.document_from_days
        if need and r.days >= need:
            yield Hit("LEAVE_SICK_DOC", "leave_request", r.ref, f"{r.ref}: {r.days:g} days sick leave, no medical certificate",
                      r.employee_id, r.employee.org_unit_code, r.end_date + timedelta(days=7))


def _develop(db: Session, today: date) -> Iterator[Hit]:
    from ..models import EmployeeSkill, OnboardingCase, ProbationReview, Skill
    from .skills import matrix, required

    active = list(db.scalars(select(Employee).where(Employee.status == "Active")))
    m = matrix(db, active, today)
    for s in db.scalars(select(Skill).where(Skill.critical.is_(True), Skill.active.is_(True))):
        cv = m.coverage.get(s.code)
        if cv and s.min_uo and cv["uo"] < s.min_uo:
            yield Hit("SKILL_COVERAGE", "skill", s.code, f"{s.name_en}: {cv['uo']} people at U/O, needs {s.min_uo}", org_unit=s.org_unit_code,
                      due=today + timedelta(days=60))
    by_id = {e.employee_id: e for e in active}
    for es in db.scalars(select(EmployeeSkill).where(EmployeeSkill.valid_until < today, EmployeeSkill.level >= 3)):
        e = by_id.get(es.employee_id)
        if e and es.skill_code in required(db, e.job_code):
            yield Hit("LICENCE_LAPSED", "employee_skill", f"{e.employee_id}:{es.skill_code}", f"{es.skill.name_en} lapsed on {es.valid_until}: renew before working alone",
                      e.employee_id, e.org_unit_code, today + timedelta(days=7))
    for c in db.scalars(select(OnboardingCase).where(OnboardingCase.status == "open")):
        late = [i for i in c.open_items if i.get("due") and i["due"] < today.isoformat()]
        if late:
            yield Hit("ONBOARD_LATE", "onboarding_case", c.ref, f"{c.ref}: overdue " + ", ".join(i["code"] for i in late), c.employee_id,
                      c.employee.org_unit_code, today + timedelta(days=3))
    for r in db.scalars(select(ProbationReview).where(ProbationReview.status.in_(("open", "submitted")))):
        if today > r.probation_end:
            yield Hit("PROBATION_LATE", "probation_review", r.ref, f"{r.ref}: probation ended {r.probation_end}, no decision", r.employee_id,
                      r.employee.org_unit_code, today)


def run(db: Session, today: Optional[date] = None) -> dict:
    today = today or date.today()
    from .newstaff import sync_reviews
    from .skills import generate_needs

    sync_reviews(db, today)
    generate_needs(db, today)
    hits: dict[tuple[str, str, str], Hit] = {}
    for gen in (_employees, _org, _time, _hrdev, _develop):
        for h in gen(db, today):
            hits[(h.rule, h.entity, h.entity_id)] = h
    open_issues = {
        (i.rule_code, i.entity, i.entity_id): i
        for i in db.scalars(select(DataIssue).where(DataIssue.status.in_(("open", "ignored"))))
    }
    added = closed = 0
    for key, h in hits.items():
        sev, owner, _ = RULES[h.rule]
        cur = open_issues.get(key)
        if cur:
            cur.message = h.message[:400]
            cur.due_date = h.due or cur.due_date
            continue
        db.add(
            DataIssue(
                rule_code=h.rule, severity=sev, entity=h.entity, entity_id=h.entity_id,
                employee_id=h.employee_id, org_unit_code=h.org_unit, message=h.message[:400],
                owner_role=owner, due_date=h.due or today + timedelta(days=14),
            )
        )
        added += 1
    for key, issue in open_issues.items():
        if key not in hits and issue.status == "open":
            issue.status = "fixed"
            issue.resolved_by = "system (data now passes)"
            issue.resolved_at = datetime.now()
            closed += 1
    db.flush()
    return {"added": added, "closed": closed, "open": db.scalar(select(func.count()).select_from(DataIssue).where(DataIssue.status == "open"))}
