"""Pulse surveys. Anonymity rules are in app/models/pulse.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, OrgUnit, PulseInvite, PulseResponse, PulseSurvey, User
from ..models.pulse import DEFAULT_QUESTIONS, MIN_GROUP
from .common import audit, user_scope

HR = ("hr", "hr_manager")


class PulseError(ValueError):
    pass


def dept_code(emp: Employee) -> Optional[str]:
    u = emp.org_unit
    if u is None:
        return None
    d = u.department
    return (d or u).code


def open_survey(db: Session, user: User, title: str, opens: date, closes: date, groups: list[str]) -> PulseSurvey:
    if closes < opens:
        raise PulseError("Check the dates")
    if not groups:
        raise PulseError("Choose who is asked")
    s = PulseSurvey(title=title.strip()[:80] or "Pulse survey", opens_on=opens, closes_on=closes, groups=groups,
                    questions=[dict(q) for q in DEFAULT_QUESTIONS], status="open", created_by=user.label)
    db.add(s)
    db.flush()
    n = 0
    for e in db.scalars(select(Employee).where(Employee.status == "Active", Employee.employee_group.in_(groups))):
        db.add(PulseInvite(survey_id=s.id, employee_id=e.employee_id, dept_code=dept_code(e)))
        n += 1
    db.flush()
    audit(db, user, "pulse_open", "pulse_survey", s.id, f"{s.title}: {n} invited")
    return s


def my_open(db: Session, user: User, today: Optional[date] = None) -> list[PulseSurvey]:
    """Open surveys this person is invited to and has not answered."""
    if not user.employee_id:
        return []
    today = today or date.today()
    q = (select(PulseSurvey).join(PulseInvite, PulseInvite.survey_id == PulseSurvey.id)
         .where(PulseInvite.employee_id == user.employee_id, PulseInvite.done.is_(False), PulseSurvey.status == "open",
                PulseSurvey.opens_on <= today, PulseSurvey.closes_on >= today))
    return list(db.scalars(q))


def _answers(s: PulseSurvey, form) -> dict:
    out = {}
    for q in s.questions:
        v = form.get(f"q_{q['id']}")
        if v:
            try:
                v = int(v)
            except ValueError:
                continue
            if 1 <= v <= 5:
                out[q["id"]] = v
    return out


def answer(db: Session, user: User, s: PulseSurvey, form, today: Optional[date] = None) -> None:
    today = today or date.today()
    inv = db.scalar(select(PulseInvite).where(PulseInvite.survey_id == s.id, PulseInvite.employee_id == user.employee_id))
    if inv is None or s.status != "open" or not (s.opens_on <= today <= s.closes_on):
        raise PulseError("This survey is not open for you")
    if inv.done:
        raise PulseError("You have already answered. Thank you!")
    ans = _answers(s, form)
    if len(ans) < len(s.questions) // 2:
        raise PulseError("Please answer at least half of the questions")
    emp = db.get(Employee, user.employee_id)
    db.add(PulseResponse(survey_id=s.id, dept_code=inv.dept_code, employee_group=emp.employee_group if emp else None, answers=ans,
                         comment=(form.get("comment") or "").strip()[:500] or None, on=today, source="web"))
    inv.done = True
    # no audit row with the person's name and time: it could link them to the answer


def paper(db: Session, user: User, s: PulseSurvey, form, today: Optional[date] = None) -> None:
    """HR types in one paper sheet from the box. Only the department and group are kept."""
    if not user.has(*HR):
        raise PulseError("Only HR enters paper answers")
    ans = _answers(s, form)
    if len(ans) < len(s.questions) // 2:
        raise PulseError("Please answer at least half of the questions")
    dept = form.get("dept") or None
    db.add(PulseResponse(survey_id=s.id, dept_code=dept, employee_group=form.get("group") or None, answers=ans,
                         comment=(form.get("comment") or "").strip()[:500] or None, on=today or date.today(), source="paper"))


@dataclass
class Result:
    n: int = 0
    invited: int = 0
    answered: int = 0
    by_q: dict = field(default_factory=dict)  # qid -> {avg, fav, n}
    by_dept: dict = field(default_factory=dict)  # dept -> {n, avg, fav, rate, by_q} (only groups of MIN_GROUP or more)
    hidden: int = 0  # departments hidden because they are small
    comments: list = field(default_factory=list)
    prev: dict = field(default_factory=dict)  # qid -> fav in the previous survey
    prev_title: Optional[str] = None
    avg: Optional[float] = None
    fav: Optional[int] = None


def _stats(rows: list[PulseResponse], qids: list[str]) -> tuple[dict, Optional[float], Optional[float]]:
    by_q = {}
    alls = []
    for q in qids:
        vals = [r.answers[q] for r in rows if q in r.answers]
        alls += vals
        by_q[q] = {"n": len(vals), "avg": round(sum(vals) / len(vals), 2) if vals else None,
                   "fav": round(100 * sum(1 for v in vals if v >= 4) / len(vals)) if vals else None}
    avg = round(sum(alls) / len(alls), 2) if alls else None
    fav = round(100 * sum(1 for v in alls if v >= 4) / len(alls)) if alls else None
    return by_q, avg, fav


def can_see_results(db: Session, user: User) -> bool:
    return user.has("hr", "hr_manager", "gm", "dept_head")


def results(db: Session, user: User, s: PulseSurvey) -> Result:
    qids = [q["id"] for q in s.questions]
    scope = user_scope(db, user)
    rows = list(db.scalars(select(PulseResponse).where(PulseResponse.survey_id == s.id)))
    invites = list(db.scalars(select(PulseInvite).where(PulseInvite.survey_id == s.id)))
    if not scope.all:
        rows = [r for r in rows if r.dept_code in scope.org_units]
        invites = [i for i in invites if i.dept_code in scope.org_units]
    res = Result(n=len(rows), invited=len(invites), answered=sum(1 for i in invites if i.done))
    if res.n < MIN_GROUP:
        return res  # too few answers to show anything
    res.by_q, res.avg, res.fav = _stats(rows, qids)
    depts = sorted({r.dept_code for r in rows if r.dept_code})
    for d in depts:
        dr = [r for r in rows if r.dept_code == d]
        if len(dr) < MIN_GROUP:
            res.hidden += 1
            continue
        bq, avg, fav = _stats(dr, qids)
        inv = [i for i in invites if i.dept_code == d]
        res.by_dept[d] = {"n": len(dr), "avg": avg, "fav": fav, "by_q": bq,
                          "rate": round(100 * sum(1 for i in inv if i.done) / len(inv)) if inv else None}
    if user.has(*HR):
        res.comments = sorted(r.comment for r in rows if r.comment)  # sorted, so the order gives nothing away
    prev = db.scalar(select(PulseSurvey).where(PulseSurvey.closes_on < s.opens_on).order_by(PulseSurvey.closes_on.desc()))
    if prev:
        prow = list(db.scalars(select(PulseResponse).where(PulseResponse.survey_id == prev.id)))
        if not scope.all:
            prow = [r for r in prow if r.dept_code in scope.org_units]
        if len(prow) >= MIN_GROUP:
            pq, _, _ = _stats(prow, qids)
            res.prev = {k: v["fav"] for k, v in pq.items()}
            res.prev_title = prev.title
    return res


def close(db: Session, user: User, s: PulseSurvey) -> None:
    if not user.has(*HR):
        raise PulseError("Only HR closes a survey")
    s.status = "closed"
    audit(db, user, "pulse_close", "pulse_survey", s.id, s.title)


def dept_names(db: Session) -> dict[str, str]:
    return {u.code: u.name_en for u in db.scalars(select(OrgUnit))}
