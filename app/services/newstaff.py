"""New staff: the onboarding checklist that opens when a hire is approved, and
the probation review that the Department Head completes before day 119.

Thai law: a worker dismissed after 120 days of service is owed severance, so
the decision must be made, and the notice given, inside the 119 days. The app
counts that date for each person and blocks a "fail" decided too late."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    ApprovalRequest,
    DailyAttendance,
    Employee,
    EmploymentEvent,
    OnboardingCase,
    ProbationReview,
    User,
)
from ..models.develop import PROBATION_CRITERIA
from . import approvals
from .approvals import StepSpec
from .common import audit, get_setting, next_ref
from .leave import probation_end

# code, label, owner, days after start (negative = before), when
ONBOARDING = [
    ("CONTRACT", "Employment contract signed, copy given", "hr", 0, None),
    ("DOCS", "ID card, house registration and certificates copied", "hr", 0, None),
    ("PERMIT", "Work permit and visa copied into the restricted record", "hr", 0, "foreign"),
    ("SSO", "Registered with social security (SPS 1-03, within 30 days)", "hr", 30, None),
    ("BPLUS", "Employee created in Bplus (pay, bank account)", "hr", 3, None),
    ("SCAN", "Face registered on the scanner, ID matches", "hr", 1, None),
    ("PPE", "Uniform, PPE and locker given", "dept_head", 0, "factory"),
    ("INDUCTION", "Company induction and safety induction done", "hr", 7, None),
    ("BUDDY", "Buddy or trainer named for on-the-job training", "dept_head", 1, None),
    ("IT", "Email and system accounts opened", "admin", 3, "office"),
    ("SKILLS", "First skill check recorded in the skills matrix", "dept_head", 30, None),
]

FACTORY_GROUPS = ("Operator",)


def open_onboarding(db: Session, emp: Employee, user: Optional[User]) -> OnboardingCase:
    case = db.scalar(select(OnboardingCase).where(OnboardingCase.employee_id == emp.employee_id, OnboardingCase.status == "open"))
    if case is not None:
        return case
    items = []
    for code, label, owner, days, when in ONBOARDING:
        if when == "foreign" and not emp.is_foreign_worker:
            continue
        if when == "factory" and emp.employee_group not in FACTORY_GROUPS:
            continue
        if when == "office" and emp.employee_group in FACTORY_GROUPS:
            continue
        items.append({"code": code, "label": label, "owner": owner, "due": (emp.join_date + timedelta(days=days)).isoformat(), "done": False})
    case = OnboardingCase(ref=next_ref(db, "ON", emp.join_date.year), employee_id=emp.employee_id, start_date=emp.join_date, items=items)
    case.employee = emp
    db.add(case)
    db.flush()
    audit(db, user, "create", "onboarding_case", case.ref, f"Onboarding for {emp.employee_id}, starts {emp.join_date}")
    from .skills import generate_needs

    generate_needs(db, emp_ids={emp.employee_id})
    return case


def can_tick(user: User, item: dict) -> bool:
    if user.has("hr", "hr_manager"):
        return True
    if item.get("owner") == "admin":
        return user.has("admin")
    if item.get("owner") == "dept_head":
        return user.has("dept_head", "supervisor")
    return False


def tick(db: Session, case: OnboardingCase, user: User, code: str, done: bool, note: str = "") -> None:
    items = [dict(i) for i in case.items]
    for i in items:
        if i["code"] != code:
            continue
        if not can_tick(user, i):
            raise PermissionError("Not your item")
        i["done"] = done
        i["by"] = user.label if done else None
        i["at"] = datetime.now().isoformat(timespec="minutes") if done else None
        if note:
            i["note"] = note[:200]
        audit(db, user, "tick" if done else "untick", "onboarding_case", case.ref, f"{code}: {i['label']}")
    case.items = items
    if not case.open_items:
        case.status, case.closed_at = "closed", datetime.now()


# ---------- probation ----------

def decide_by(emp: Employee) -> date:
    """Last day to give a 'did not pass' decision without severance."""
    return probation_end(emp)


def sync_reviews(db: Session, today: Optional[date] = None) -> int:
    """Open a review for everyone whose probation ends within the alert window."""
    today = today or date.today()
    window = int(get_setting(db, "probation_alert_days") or 14) + 21
    have = set(db.scalars(select(ProbationReview.employee_id)).all())
    n = 0
    for e in db.scalars(select(Employee).where(Employee.status == "Active")):
        end = probation_end(e)
        if e.employee_id in have or end < today - timedelta(days=30) or end > today + timedelta(days=window):
            continue
        if e.employee_group not in ("Operator", "Staff", "Manager"):
            continue
        db.add(ProbationReview(ref=next_ref(db, "PR", end.year), employee_id=e.employee_id, probation_end=end, status="open"))
        n += 1
    db.flush()
    return n


def facts(db: Session, emp: Employee, until: date) -> dict:
    rows = db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.employee_id,
                                                   DailyAttendance.work_date >= emp.join_date, DailyAttendance.work_date <= until)).all()
    work = [a for a in rows if not a.is_rest_day]
    from .skills import matrix

    m = matrix(db, [emp])
    req = [c for c in m.cells.values() if c["need"]]
    return {
        "workdays": len(work),
        "no_show": sum(1 for a in work if a.day_code == "a"),
        "sick": sum(1 for a in work if a.day_code == "s"),
        "late": sum(1 for a in work if a.late_minutes > 5),
        "skills_met": sum(1 for c in req if not c["gap"]),
        "skills_needed": len(req),
    }


class ProbationError(Exception):
    pass


def submit(db: Session, rv: ProbationReview, user: User, scores: dict, recommendation: str, comment: str, today: Optional[date] = None) -> ApprovalRequest:
    today = today or date.today()
    if rv.status != "open":
        raise ProbationError("This review was already sent")
    if recommendation not in ("pass", "fail"):
        raise ProbationError("Choose pass or fail")
    emp = rv.employee
    if recommendation == "fail":
        if not (comment or "").strip():
            raise ProbationError("Give the reasons for a fail: the employee has the right to know them")
        if today > decide_by(emp):
            raise ProbationError(f"Too late for a fail without severance: the probation ended on {decide_by(emp)}. Talk to the HR Manager.")
    clean = {}
    for k, _ in PROBATION_CRITERIA:
        try:
            v = int(scores.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if not 1 <= v <= 5:
            raise ProbationError("Score every line from 1 to 5")
        clean[k] = v
    rv.scores, rv.recommendation, rv.comment = clean, recommendation, (comment or "")[:3000]
    rv.facts = facts(db, emp, today)
    rv.reviewed_by, rv.reviewed_at = user.label, datetime.now()
    rv.status = "submitted"
    steps = [StepSpec("dept_head", head_of=emp.org_unit_code), StepSpec("hr_check", role="hr")]
    if recommendation == "fail":
        steps.append(StepSpec("hr_manager", role="hr_manager"))
    appr = approvals.submit(
        db, request_type="PROBATION", entity="probation_review", entity_id=str(rv.id),
        title=f"Probation {'pass' if recommendation == 'pass' else 'FAIL'} {emp.employee_id} (ends {rv.probation_end:%d/%m/%Y})",
        steps=steps, user=user, org_unit_code=emp.org_unit_code, employee_id=emp.employee_id,
    )
    rv.approval_id = appr.id
    audit(db, user, "submit", "probation_review", rv.ref, f"Recommendation: {recommendation}")
    return appr


@approvals.on_close("PROBATION")
def _probation_close(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    rv = db.get(ProbationReview, int(appr.entity_id))
    if rv is None:
        return
    if outcome != "approved":
        rv.status = "open"  # back to the Department Head
        return
    emp = rv.employee
    if rv.recommendation == "pass":
        rv.status = "passed"
        db.add(EmploymentEvent(employee_id=emp.employee_id, event_type="probation_pass", effective_date=rv.probation_end + timedelta(days=1),
                               approval_id=appr.id, created_by=appr.submitted_by, applied=True, note=rv.ref))
        if emp.probation_end_date is None:
            emp.probation_end_date = rv.probation_end
    else:
        rv.status = "failed"
        last = min(rv.probation_end, max(date.today(), emp.join_date))
        ev = EmploymentEvent(employee_id=emp.employee_id, event_type="probation_fail", effective_date=last, reason_code="PROBATION",
                             approval_id=appr.id, created_by=appr.submitted_by, note=rv.ref)
        db.add(ev)
        db.flush()
        from . import exits
        from .people import apply_due_events

        exits.open_case(db, emp, last, "PROBATION", user, appr.id)
        apply_due_events(db, date.today(), employee_id=emp.employee_id)
    db.flush()
    audit(db, user, "close", "probation_review", rv.ref, rv.status)


def averages(rv: ProbationReview) -> Optional[float]:
    if not rv.scores:
        return None
    return round(sum(rv.scores.values()) / len(rv.scores), 1)


def due_count(db: Session, emp_ids: Optional[set[str]] = None) -> int:
    q = select(func.count()).select_from(ProbationReview).where(ProbationReview.status == "open")
    if emp_ids is None:
        return db.scalar(q) or 0
    return sum(1 for r in db.scalars(select(ProbationReview).where(ProbationReview.status == "open")) if r.employee_id in emp_ids)
