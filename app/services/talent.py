"""Talent review (9-box). See app/models/talent.py for the rules of use."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appraisal, Employee, PerformanceCycle, TalentReview, User
from ..models.talent import BOXES, NEXT_MOVES, POTENTIAL_QUESTIONS, RISKS, box_of, perf_level
from .common import audit, user_scope

HR = ("hr", "hr_manager")
AGREE = ("hr_manager", "gm")


class TalentError(ValueError):
    pass


def released_cycles(db: Session) -> list[PerformanceCycle]:
    return list(db.scalars(select(PerformanceCycle).where(PerformanceCycle.released_at.is_not(None)).order_by(PerformanceCycle.period_end.desc())))


def years(db: Session) -> list[int]:
    return sorted({c.period_end.year for c in released_cycles(db)}, reverse=True)


def rating_for(db: Session, emp_id: str, year: int) -> Optional[Appraisal]:
    """The released appraisal that feeds the performance axis for this review year."""
    return db.scalar(select(Appraisal).join(PerformanceCycle, Appraisal.cycle_id == PerformanceCycle.id)
                     .where(Appraisal.employee_id == emp_id, Appraisal.status == "released", Appraisal.final_rating.is_not(None),
                            PerformanceCycle.released_at.is_not(None))
                     .where(PerformanceCycle.period_end >= date(year, 1, 1), PerformanceCycle.period_end <= date(year, 12, 31))
                     .order_by(PerformanceCycle.period_end.desc()))


def can_view(db: Session, user: User, emp: Employee) -> bool:
    if user.employee_id == emp.employee_id:
        return False  # the person under review never sees their own box
    return user_scope(db, user).allows_unit(emp.org_unit_code)


def can_propose(db: Session, user: User, emp: Employee) -> bool:
    # auditors look but do not judge
    return can_view(db, user, emp) and not (user.has("auditor") and not user.has(*HR, "gm"))


def can_agree(user: User, emp: Employee) -> bool:
    return user.has(*AGREE) and user.employee_id != emp.employee_id


def people(db: Session, user: User, year: int) -> list[tuple[Employee, Optional[Appraisal], Optional[TalentReview]]]:
    """Everyone in the user's scope with a released rating for the year, plus their review if started."""
    scope = user_scope(db, user)
    reviews = {t.employee_id: t for t in db.scalars(select(TalentReview).where(TalentReview.year == year))}
    out = []
    for e in db.scalars(select(Employee).where(Employee.status == "Active").order_by(Employee.org_unit_code, Employee.employee_id)):
        if e.employee_id == user.employee_id or not scope.allows_unit(e.org_unit_code):
            continue
        a = rating_for(db, e.employee_id, year)
        if a is None and e.employee_id not in reviews:
            continue
        out.append((e, a, reviews.get(e.employee_id)))
    return out


def grid(rows) -> dict[int, list]:
    g: dict[int, list] = {n: [] for n in range(1, 10)}
    for e, a, t in rows:
        if t and t.box:
            g[t.box].append((e, t))
    return g


def get_or_new(db: Session, year: int, emp: Employee) -> TalentReview:
    t = db.scalar(select(TalentReview).where(TalentReview.year == year, TalentReview.employee_id == emp.employee_id))
    if t is None:
        t = TalentReview(year=year, employee_id=emp.employee_id, org_unit_code=emp.org_unit_code, answers={}, status="draft")
    a = rating_for(db, emp.employee_id, year)
    if a is not None and t.status != "agreed":
        t.appraisal_id, t.rating, t.performance = a.id, a.final_rating, perf_level(a.final_rating)
    return t


def _lvl(v) -> Optional[int]:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v in (1, 2, 3) else None


def potential_from(answers: dict) -> Optional[int]:
    vals = [answers.get(k) for k in POTENTIAL_QUESTIONS]
    if any(v is None for v in vals):
        return None
    avg = sum(vals) / len(vals)
    return 3 if avg >= 2.5 else 2 if avg >= 1.5 else 1


def save(db: Session, user: User, t: TalentReview, emp: Employee, form, submit: bool) -> None:
    if not can_propose(db, user, emp):
        raise TalentError("You cannot review this person")
    if t.status == "agreed" and not can_agree(user, emp):
        raise TalentError("This review is already agreed")
    t.answers = {k: _lvl(form.get(f"q_{k}")) for k in POTENTIAL_QUESTIONS}
    t.potential = potential_from(t.answers)
    t.risk = form.get("risk") if form.get("risk") in RISKS else None
    t.risk_reason = (form.get("risk_reason") or "").strip()[:300] or None
    t.next_move = form.get("next_move") if form.get("next_move") in NEXT_MOVES else None
    t.action = (form.get("action") or "").strip() or None
    b = box_of(t.performance, t.potential)
    t.box = b[0] if b else None
    if submit:
        if t.performance is None:
            raise TalentError("No released appraisal rating for this year, so the person cannot be placed yet")
        if t.potential is None or not t.risk or not t.next_move:
            raise TalentError("Answer the three potential questions, retention risk and next step")
        if t.risk == "high" and not t.risk_reason:
            raise TalentError("Say why the retention risk is high")
        if t.status == "draft":
            t.status = "proposed"
        t.proposed_by, t.proposed_at = user.label, datetime.now()
    db.add(t)
    db.flush()
    audit(db, user, "talent_save", "talent_review", t.id, f"{emp.employee_id} box {t.box or '-'} ({t.status})")


def agree(db: Session, user: User, t: TalentReview, emp: Employee, note: str, potential: Optional[int] = None) -> None:
    if not can_agree(user, emp):
        raise TalentError("Only the HR Manager or GM agrees talent reviews, and never their own")
    if t.status != "proposed":
        raise TalentError("Only a proposed review can be agreed")
    if potential is not None and potential != t.potential:
        if not note:
            raise TalentError("Write why the meeting changed the potential")
        t.potential = potential
        t.box = box_of(t.performance, t.potential)[0]
    t.status, t.agreed_by, t.agreed_at, t.review_note = "agreed", user.label, datetime.now(), (note or None)
    audit(db, user, "talent_agree", "talent_review", t.id, f"{emp.employee_id} box {t.box}")


def box_info(n: int):
    for (p, q), b in BOXES.items():
        if b[0] == n:
            return {"perf": p, "pot": q, "name": b[1], "action": b[2]}
    return None
