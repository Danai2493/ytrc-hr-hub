"""Internal job board and move criteria (blueprint: internal mobility, promotion criteria).

Open manpower requests that HR marks "internal" are shown to every employee.
An employee raises their hand; HR talks with them first and then tells the
current supervisor, so nobody is afraid to ask. The move itself is a normal
transfer or promotion event with the usual approvals.

Move criteria are checks, not a gate: they tell the approvers what is met and
what is not (seat, skills, rating, time in grade, written warnings) so the
decision is made on facts. Pay is never part of it."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, EmployeeSkill, EmploymentEvent, Grade, JobSkill, Position, Requisition, Skill, User
from ..models.recruit import OPEN_INTEREST, InternalInterest
from .common import audit, get_setting
from .plan import released_ratings
from .skills import effective_level

PROBATION_DAYS = 119
MOVE_TYPES = ("transfer", "promotion", "demotion", "grade_change")


class JobsError(Exception):
    pass


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


def _months(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month - (1 if b.day < a.day else 0)


def grade_since(db: Session, emp: Employee) -> date:
    """The day the person reached their current grade: the last event that changed it, else the join date."""
    evs = db.scalars(select(EmploymentEvent).where(EmploymentEvent.employee_id == emp.employee_id, EmploymentEvent.applied.is_(True),
                                                   EmploymentEvent.to_grade == emp.grade_code)
                     .order_by(EmploymentEvent.effective_date.desc())).all()
    for ev in evs:
        if ev.from_grade != ev.to_grade:
            return ev.effective_date
    return emp.join_date


def job_since(emp: Employee) -> date:
    return emp.position_since or emp.join_date


def move_checks(db: Session, emp: Employee, to_position: Optional[str], to_job: Optional[str] = None, to_grade: Optional[str] = None,
                event_type: str = "transfer", today: Optional[date] = None) -> list[dict]:
    """What the approvers should know about moving this person. Levels: ok, info, flag (look again), block (cannot)."""
    today = today or date.today()
    out: list[dict] = []
    pos = db.get(Position, to_position) if to_position else None
    job_code = to_job or (pos.job_code if pos else None) or emp.job_code
    grade_code = to_grade or (pos.grade_code if pos else None) or emp.grade_code

    if (today - emp.join_date).days < PROBATION_DAYS:
        out.append(finding("flag", "mv_probation", until=date.fromordinal(emp.join_date.toordinal() + PROBATION_DAYS).strftime("%d/%m/%Y")))

    # 1. A seat: budgeted, not frozen, not full
    if pos is not None and pos.position_id != emp.position_id:
        if pos.frozen:
            out.append(finding("block", "mv_seat_frozen", pos=pos.position_id))
        elif not pos.budgeted:
            out.append(finding("flag", "mv_seat_unbudgeted", pos=pos.position_id))
        elif pos.filled >= pos.seats:
            out.append(finding("flag", "mv_seat_full", pos=pos.position_id, filled=pos.filled, seats=pos.seats))
        else:
            out.append(finding("ok", "mv_seat_ok", pos=pos.position_id, free=pos.seats - pos.filled))

    # 2. Grade direction and size of the step
    g_now = db.get(Grade, emp.grade_code) if emp.grade_code else None
    g_new = db.get(Grade, grade_code) if grade_code else None
    if g_now and g_new and g_now.code != g_new.code:
        ranks = sorted({g.rank for g in db.scalars(select(Grade))})
        steps = ranks.index(g_new.rank) - ranks.index(g_now.rank) if g_now.rank in ranks and g_new.rank in ranks else 0
        if event_type == "promotion" and steps < 0:
            out.append(finding("flag", "mv_grade_down", now=g_now.code, new=g_new.code))
        if event_type in ("transfer", "grade_change") and steps > 0:
            out.append(finding("info", "mv_grade_up_transfer", now=g_now.code, new=g_new.code))
        if steps > 1:
            out.append(finding("flag", "mv_grade_jump", now=g_now.code, new=g_new.code, n=steps))
    elif event_type == "promotion" and (g_now is None or g_new is None or g_now.code == g_new.code):
        out.append(finding("flag", "mv_promo_same_grade"))

    # 3. Time in grade (promotions) or in the current job (moves)
    if event_type == "promotion":
        need = int(get_setting(db, "promo_min_months"))
        m = _months(grade_since(db, emp), today)
        if m < need:
            out.append(finding("flag", "mv_time_in_grade", months=m, need=need))
        else:
            out.append(finding("ok", "mv_time_in_grade_ok", months=m))
    elif pos is not None and pos.position_id != emp.position_id:
        need = int(get_setting(db, "internal_min_months"))
        m = _months(job_since(emp), today)
        if m < need:
            out.append(finding("info", "mv_time_in_job", months=m, need=need))

    # 4. Rating: the last released one
    ratings = released_ratings(db, emp.employee_id)
    if not ratings:
        out.append(finding("info", "mv_no_rating"))
    else:
        letter = "EDCBA"[ratings[0] - 1]
        if ratings[0] <= 2:
            out.append(finding("flag" if event_type == "promotion" else "info", "mv_rating_low", rating=letter))
        else:
            out.append(finding("ok", "mv_rating_ok", rating=letter))

    # 5. Written warning in force
    from .relations import warnings_in_force

    w = warnings_in_force(db, emp.employee_id, today)
    if w:
        out.append(finding("flag", "mv_warning", until=max(x.warning_until for x in w).strftime("%d/%m/%Y")))

    # 6. Skills the new job needs (ILUO)
    if job_code and job_code != emp.job_code:
        needs = db.execute(select(JobSkill, Skill).join(Skill, Skill.code == JobSkill.skill_code).where(JobSkill.job_code == job_code)).all()
        if needs:
            have = {s.skill_code: s for s in db.scalars(select(EmployeeSkill).where(EmployeeSkill.employee_id == emp.employee_id))}
            gaps = [(sk, js.level, effective_level(have.get(sk.code), today)) for js, sk in needs
                    if effective_level(have.get(sk.code), today) < js.level]
            crit = [g for g in gaps if g[0].critical]
            if not gaps:
                out.append(finding("ok", "mv_skills_ok", n=len(needs)))
            else:
                names = ", ".join(g[0].name_en for g in gaps[:4]) + ("…" if len(gaps) > 4 else "")
                out.append(finding("flag" if crit else "info", "mv_skill_gap", n=len(gaps), of=len(needs), names=names))
    return out


def worst(checks: list[dict]) -> str:
    order = {"block": 3, "flag": 2, "info": 1, "ok": 0}
    return max((c["level"] for c in checks), key=lambda l: order.get(l, 0), default="ok")


# ---------------- the board ----------------

def postings(db: Session, today: Optional[date] = None) -> list[Requisition]:
    today = today or date.today()
    rows = db.scalars(select(Requisition).where(Requisition.status == "open", Requisition.internal.is_(True))
                      .order_by(Requisition.needed_by)).all()
    return [r for r in rows if r.open_seats > 0 and (r.internal_until is None or r.internal_until >= today)]


def my_interests(db: Session, emp_id: str) -> dict[int, InternalInterest]:
    return {i.requisition_id: i for i in db.scalars(select(InternalInterest).where(InternalInterest.employee_id == emp_id))}


def can_raise(db: Session, emp: Optional[Employee], req: Requisition, today: Optional[date] = None) -> Optional[str]:
    """Why this person cannot raise their hand (None = they can)."""
    today = today or date.today()
    if emp is None or emp.status != "Active":
        return "Only active employees can apply for internal jobs"
    if (today - emp.join_date).days < PROBATION_DAYS:
        return "You can apply for internal jobs after your probation"
    if req.position_id and req.position_id == emp.position_id:
        return "This is your current position"
    return None


def raise_hand(db: Session, user: User, req: Requisition, note: str, today: Optional[date] = None) -> InternalInterest:
    today = today or date.today()
    emp = db.get(Employee, user.employee_id) if user.employee_id else None
    if req not in postings(db, today):
        raise JobsError("This job is not open for internal applications")
    why = can_raise(db, emp, req, today)
    if why:
        raise JobsError(why)
    ii = db.scalar(select(InternalInterest).where(InternalInterest.requisition_id == req.id, InternalInterest.employee_id == emp.employee_id))
    if ii and ii.status in OPEN_INTEREST:
        raise JobsError("You have already raised your hand for this job")
    if ii is None:
        ii = InternalInterest(requisition_id=req.id, employee_id=emp.employee_id)
        db.add(ii)
    ii.status = "interested"
    ii.note = (note or "").strip()[:1000] or None
    ii.checks = move_checks(db, emp, req.position_id, req.job_code, req.grade_code, _event_type(db, emp, req), today)
    ii.hr_note = ii.decided_by = None
    ii.supervisor_told_on = None
    db.flush()
    audit(db, user, "raise_hand", "requisition", req.ref, f"{emp.employee_id} interested")
    return ii


def withdraw(db: Session, user: User, ii: InternalInterest) -> None:
    if ii.employee_id != user.employee_id:
        raise JobsError("Only the employee can withdraw")
    if ii.status not in ("interested", "talking"):
        raise JobsError("This can no longer be withdrawn here; talk with HR")
    ii.status = "withdrawn"
    audit(db, user, "withdraw", "requisition", ii.requisition.ref, f"{ii.employee_id} withdrew")


def _event_type(db: Session, emp: Employee, req: Requisition) -> str:
    g_now = db.get(Grade, emp.grade_code) if emp.grade_code else None
    g_new = db.get(Grade, req.grade_code) if req.grade_code else None
    return "promotion" if g_now and g_new and g_new.rank > g_now.rank else "transfer"


def suggested_event(db: Session, ii: InternalInterest) -> str:
    return _event_type(db, ii.employee, ii.requisition)


def interests_for(db: Session, req: Requisition, today: Optional[date] = None) -> list[InternalInterest]:
    """Refresh the checks for open hands so HR sees today's facts."""
    rows = list(req.interests)
    for ii in rows:
        if ii.status in ("interested", "talking"):
            ii.checks = move_checks(db, ii.employee, req.position_id, req.job_code, req.grade_code, _event_type(db, ii.employee, req), today)
    return rows


def hr_update(db: Session, user: User, ii: InternalInterest, status: str, note: str, told: bool, today: Optional[date] = None) -> None:
    if not user.has("hr", "hr_manager"):
        raise JobsError("Only HR updates internal applications")
    if status not in ("interested", "talking", "not_now"):
        raise JobsError("Unknown status")
    if ii.status in ("moved", "withdrawn", "moving"):
        raise JobsError("This application is closed or waiting for approval")
    if status == "not_now" and not (note or "").strip():
        raise JobsError("Give the reason you will share with the employee")
    ii.status = status
    ii.hr_note = (note or "").strip()[:300] or ii.hr_note
    ii.decided_by = user.label
    if told and not ii.supervisor_told_on:
        ii.supervisor_told_on = today or date.today()
    audit(db, user, "internal_" + status, "requisition", ii.requisition.ref, f"{ii.employee_id}: {ii.hr_note or ''}")


def link_move(db: Session, ii: InternalInterest, approval_id: int) -> None:
    ii.status = "moving"
    ii.approval_id = approval_id


def on_move_closed(db: Session, interest_id: int, outcome: str) -> None:
    ii = db.get(InternalInterest, interest_id)
    if ii is None:
        return
    if outcome == "approved":
        ii.status = "moved"
        req = ii.requisition
        req.hired = (req.hired or 0) + 1
        if req.open_seats == 0:
            req.status = "filled"
            req.closed_on = date.today()
    else:
        ii.status = "talking"
        ii.approval_id = None


def open_count(db: Session) -> int:
    return len(db.scalars(select(InternalInterest).where(InternalInterest.status.in_(("interested",)))).all())
