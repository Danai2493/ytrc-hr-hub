"""Phase 5, plan ahead: succession, leaving-risk flags, workforce plan.
Rules of use are in app/models/plan.py."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (Appraisal, DailyAttendance, Employee, EmploymentEvent, ErCase, Grade, OrgUnit, OTRequest, PerformanceCycle, Position,
                      Requisition, StayTalk, SuccessionPlan, Successor, TalentReview, TrainingRecord, User)
from ..models.plan import READINESS, VACANCY_RISK
from .common import audit, get_setting, user_scope

RETIRE_AGE = 60  # Admin setting retire_age


class PlanError(ValueError):
    pass


def retire_age(db: Session) -> int:
    try:
        return int(get_setting(db, "retire_age") or RETIRE_AGE)
    except (TypeError, ValueError):
        return RETIRE_AGE


def retire_on(db: Session, e: Employee) -> Optional[date]:
    if not e.birth_date:
        return None
    a = retire_age(db)
    try:
        return e.birth_date.replace(year=e.birth_date.year + a)
    except ValueError:  # 29 February
        return date(e.birth_date.year + a, 3, 1)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def dept_of_unit(db: Session, code: Optional[str], _cache: dict = {}) -> Optional[str]:  # noqa: B006 - small per-process cache
    if not code:
        return None
    u = db.get(OrgUnit, code)
    if u is None:
        return code
    d = u.department
    return (d or u).code


def latest_talent(db: Session, emp_id: str) -> Optional[TalentReview]:
    return db.scalar(select(TalentReview).where(TalentReview.employee_id == emp_id, TalentReview.status.in_(("proposed", "agreed")))
                     .order_by(TalentReview.year.desc()))


def released_ratings(db: Session, emp_id: str) -> list[int]:
    rows = db.execute(select(Appraisal.final_rating).join(PerformanceCycle, Appraisal.cycle_id == PerformanceCycle.id)
                      .where(Appraisal.employee_id == emp_id, Appraisal.status == "released", Appraisal.final_rating.is_not(None))
                      .order_by(PerformanceCycle.period_end.desc())).scalars().all()
    return list(rows)


# ---------------- succession ----------------

def can_see_succession(db: Session, user: User, pos: Position) -> bool:
    if user.employee_id and any(e.employee_id == user.employee_id for e in pos.incumbents):
        return False  # you do not plan your own successor here; your manager and HR do
    return user_scope(db, user).allows_unit(pos.org_unit_code) and user.has("hr", "hr_manager", "gm", "dept_head", "auditor")


def can_edit_succession(db: Session, user: User, pos: Position) -> bool:
    return can_see_succession(db, user, pos) and user.has("hr", "hr_manager", "gm", "dept_head")


def critical_positions(db: Session, user: User) -> list[Position]:
    rows = db.scalars(select(Position).where(Position.active.is_(True), Position.critical.is_(True)).order_by(Position.org_unit_code, Position.position_id))
    return [p for p in rows if can_see_succession(db, user, p)]


def plan_for(db: Session, pos: Position) -> SuccessionPlan:
    p = db.scalar(select(SuccessionPlan).where(SuccessionPlan.position_id == pos.position_id))
    return p or SuccessionPlan(position_id=pos.position_id, successors=[])


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


def position_flags(db: Session, pos: Position, plan: SuccessionPlan, today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    out = []
    succ = [s for s in plan.successors if s.employee.status == "Active"]
    if not succ:
        out.append(finding("block", "sp_none"))
    elif not any(s.readiness == "now" for s in succ):
        out.append(finding("flag", "sp_none_ready"))
    if len(succ) == 1:
        out.append(finding("info", "sp_single"))
    for e in pos.incumbents:
        r = retire_on(db, e)
        if r and r <= today + timedelta(days=730):
            out.append(finding("flag", "sp_retire", emp=e.employee_id, on=r.strftime("%d/%m/%Y")))
        t = latest_talent(db, e.employee_id)
        if t and t.risk == "high":
            out.append(finding("flag", "sp_incumbent_risk", emp=e.employee_id))
    if pos.filled < pos.seats:
        out.append(finding("flag", "sp_vacant"))
    return out


def suggestions(db: Session, pos: Position, plan: SuccessionPlan) -> list[tuple[Employee, list[str]]]:
    """People worth a look: strong talent review results nearby, or a career path that leads here."""
    taken = {s.employee_id for s in plan.successors} | {e.employee_id for e in pos.incumbents}
    dept = dept_of_unit(db, pos.org_unit_code)
    fam = pos.job.job_family if pos.job else None
    prank = pos.grade.rank if pos.grade else 99
    out = []
    for e in db.scalars(select(Employee).where(Employee.status == "Active")):
        if e.employee_id in taken:
            continue
        why = []
        if e.job and pos.job_code and e.job.next_job_code == pos.job_code:
            why.append("career path")
        t = latest_talent(db, e.employee_id)
        if t and t.box in (6, 8, 9):
            why.append(f"9-box {t.box}")
        if t and t.next_move in ("ready_now", "ready_2y"):
            why.append("ready for a bigger role")
        if not why:
            continue
        near = dept_of_unit(db, e.org_unit_code) == dept or (fam and e.job and e.job.job_family == fam)
        erank = db.get(Grade, e.grade_code).rank if e.grade_code and db.get(Grade, e.grade_code) else 0
        if not near or erank >= prank:
            continue
        out.append((e, why))
    return sorted(out, key=lambda x: -len(x[1]))[:8]


def save_plan(db: Session, user: User, pos: Position, form) -> SuccessionPlan:
    if not can_edit_succession(db, user, pos):
        raise PlanError("You cannot change this plan")
    plan = plan_for(db, pos)
    plan.impact = form.get("impact") if form.get("impact") in VACANCY_RISK else None
    plan.cover = (form.get("cover") or "").strip()[:300] or None
    plan.note = (form.get("note") or "").strip() or None
    plan.reviewed_by, plan.reviewed_on = user.label, date.today()
    db.add(plan)
    db.flush()
    audit(db, user, "succession_save", "position", pos.position_id, "plan reviewed")
    return plan


def add_successor(db: Session, user: User, pos: Position, emp_id: str, readiness: str, gaps: str) -> None:
    if not can_edit_succession(db, user, pos):
        raise PlanError("You cannot change this plan")
    e = db.get(Employee, (emp_id or "").strip().upper())
    if e is None or e.status != "Active":
        raise PlanError("Employee not found")
    if e in pos.incumbents:
        raise PlanError("This person already holds the position")
    if readiness not in READINESS:
        raise PlanError("Choose how ready they are")
    plan = plan_for(db, pos)
    if plan.id is None:
        db.add(plan)
        db.flush()
    if any(s.employee_id == e.employee_id for s in plan.successors):
        raise PlanError("Already on the list")
    plan.successors.append(Successor(employee_id=e.employee_id, readiness=readiness, rank=len(plan.successors) + 1,
                                     gaps=(gaps or "").strip()[:300] or None, added_by=user.label))
    plan.reviewed_by, plan.reviewed_on = user.label, date.today()
    audit(db, user, "successor_add", "position", pos.position_id, f"{e.employee_id} {readiness}")


def remove_successor(db: Session, user: User, pos: Position, sid: int) -> None:
    if not can_edit_succession(db, user, pos):
        raise PlanError("You cannot change this plan")
    s = db.get(Successor, sid)
    if s is None or s.plan.position_id != pos.position_id:
        raise PlanError("Not found")
    db.delete(s)
    audit(db, user, "successor_remove", "position", pos.position_id, s.employee_id)


def bench(db: Session, positions: list[Position]) -> dict:
    out = {"positions": len(positions), "covered_now": 0, "covered_2y": 0, "none": 0}
    for p in positions:
        plan = plan_for(db, p)
        r = {s.readiness for s in plan.successors if s.employee.status == "Active"}
        if "now" in r:
            out["covered_now"] += 1
        if r & {"now", "1_2y"}:
            out["covered_2y"] += 1
        if not r:
            out["none"] += 1
    return out


# ---------------- leaving-risk flags ----------------

SIGNALS = {
    "talent_high": (4, "Manager rated retention risk high in the talent review"),
    "talent_medium": (1, "Manager rated retention risk medium in the talent review"),
    "complaint": (2, "Raised a complaint that is still open"),
    "absence_up": (2, "Absent {n} days in the last 60 days, up from {m}"),
    "stuck": (2, "Rated A or B but no promotion or grade change for {years} years"),
    "contract_end": (2, "Contract ends on {on}"),
    "new_joiner": (1, "In the first year (joined {on}): most leavers go in year one"),
    "late_up": (1, "Late {n} times in the last 60 days"),
    "no_training": (1, "No training in the last 12 months"),
    "rating_drop": (1, "Rating fell from {a} to {b}"),
    "heavy_ot": (1, "OT about {h} hours a month over the last 3 months"),
    "warning": (1, "A written warning is in force"),
}
HOW = {  # the rule behind each signal, for the "how the score works" table
    "talent_high": "Talent review: retention risk high",
    "talent_medium": "Talent review: retention risk medium",
    "complaint": "Their own complaint or grievance is still open",
    "absence_up": "3 or more absences in the last 60 days, more than the 60 days before",
    "stuck": "Last rating A or B, no promotion or grade change for over 4 years",
    "contract_end": "Fixed-term contract ends within 90 days",
    "new_joiner": "Joined less than a year ago",
    "late_up": "Late 5 times or more in the last 60 days",
    "no_training": "No training in 12 months (after the first year)",
    "rating_drop": "Latest rating lower than the one before",
    "heavy_ot": "OT averaging 50 hours a month or more over 3 months",
    "warning": "A written warning is in force",
}
LETTER = {5: "A", 4: "B", 3: "C", 2: "D", 1: "E"}


@dataclass
class Risk:
    employee: Employee
    signals: list = field(default_factory=list)  # finding dicts with a weight
    score: int = 0
    level: str = "low"
    last_talk: Optional[StayTalk] = None


def signals_for(db: Session, e: Employee, today: date) -> list[tuple]:
    out = []
    t = latest_talent(db, e.employee_id)
    if t and t.risk == "high":
        out.append(("talent_high", {}))
    elif t and t.risk == "medium":
        out.append(("talent_medium", {}))
    if db.scalar(select(ErCase.id).where(ErCase.employee_id == e.employee_id, ErCase.kind.in_(("grievance", "harassment")), ErCase.status != "closed")):
        out.append(("complaint", {}))
    days = list(db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == e.employee_id, DailyAttendance.work_date > today - timedelta(days=120))))
    now = [d for d in days if d.work_date > today - timedelta(days=60)]
    before = [d for d in days if d.work_date <= today - timedelta(days=60)]
    a_now, a_before = sum(1 for d in now if d.day_code == "a"), sum(1 for d in before if d.day_code == "a")
    if a_now >= 3 and a_now > a_before:
        out.append(("absence_up", {"n": a_now, "m": a_before}))
    late = sum(1 for d in now if (d.late_minutes or 0) > 0)
    if late >= 5:
        out.append(("late_up", {"n": late}))
    ratings = released_ratings(db, e.employee_id)
    if ratings and ratings[0] >= 4:
        last_move = db.scalar(select(EmploymentEvent.effective_date).where(EmploymentEvent.employee_id == e.employee_id,
                                                                            EmploymentEvent.event_type.in_(("promotion", "grade_change")))
                              .order_by(EmploymentEvent.effective_date.desc()))
        since = last_move or e.join_date
        if since and (today - since).days > 4 * 365:
            out.append(("stuck", {"years": (today - since).days // 365}))
    if len(ratings) >= 2 and ratings[0] < ratings[1]:
        out.append(("rating_drop", {"a": LETTER[ratings[1]], "b": LETTER[ratings[0]]}))
    if e.contract_end_date and today <= e.contract_end_date <= today + timedelta(days=90):
        out.append(("contract_end", {"on": e.contract_end_date.strftime("%d/%m/%Y")}))
    if e.join_date and (today - e.join_date).days < 365:
        out.append(("new_joiner", {"on": e.join_date.strftime("%d/%m/%Y")}))
    elif not db.scalar(select(TrainingRecord.id).where(TrainingRecord.employee_id == e.employee_id, TrainingRecord.on_date > today - timedelta(days=365))):
        out.append(("no_training", {}))
    mins = db.scalars(select(OTRequest.requested_minutes).where(OTRequest.employee_id == e.employee_id, OTRequest.work_date > today - timedelta(days=91),
                                                                 OTRequest.status.in_(("approved", "verified", "paid")))).all()
    h = sum(mins) / 60 / 3
    if h >= 50:
        out.append(("heavy_ot", {"h": round(h)}))
    if db.scalar(select(ErCase.id).where(ErCase.employee_id == e.employee_id, ErCase.outcome == "written", ErCase.decided_at.is_not(None),
                                         ErCase.warning_until >= today)):
        out.append(("warning", {}))
    return [{"code": "rk_" + c, "params": p, "weight": SIGNALS[c][0], "level": "flag" if SIGNALS[c][0] >= 2 else "info"} for c, p in out]


HIGH, WATCH = 6, 4  # points; tune once real leavers show which signals matter


def level_of(score: int) -> str:
    return "high" if score >= HIGH else "watch" if score >= WATCH else "low"


def can_see_risk(user: User) -> bool:
    return user.has("hr", "hr_manager", "gm")


def risks(db: Session, user: User, today: Optional[date] = None, min_level: str = "watch") -> list[Risk]:
    today = today or date.today()
    out = []
    for e in db.scalars(select(Employee).where(Employee.status == "Active").order_by(Employee.employee_id)):
        if e.employee_id == user.employee_id:
            continue
        sig = signals_for(db, e, today)
        score = sum(x["weight"] for x in sig)
        level = level_of(score)
        if min_level == "watch" and level == "low":
            continue
        talk = db.scalar(select(StayTalk).where(StayTalk.employee_id == e.employee_id).order_by(StayTalk.on.desc()))
        out.append(Risk(e, sig, score, level, talk))
    return sorted(out, key=lambda r: (-r.score, r.employee.employee_id))


def add_talk(db: Session, user: User, emp_id: str, on: date, note: str, follow_up: Optional[date]) -> StayTalk:
    if not can_see_risk(user):
        raise PlanError("Only HR records stay talks")
    if user.employee_id == emp_id:
        raise PlanError("You cannot record a talk about yourself")
    if db.get(Employee, emp_id) is None:
        raise PlanError("Employee not found")
    if len(note.strip()) < 10:
        raise PlanError("Write what was said and agreed")
    t = StayTalk(employee_id=emp_id, on=on, by=user.label, note=note.strip(), follow_up_on=follow_up)
    db.add(t)
    audit(db, user, "stay_talk", "employee", emp_id, "stay talk recorded")
    return t


# ---------------- workforce plan ----------------

@dataclass
class Forecast:
    months: list = field(default_factory=list)  # first day of each month
    rows: dict = field(default_factory=dict)  # dept -> {start, budget, projected[], known[], expected[], hires[], gap[], rate}
    total: dict = field(default_factory=dict)
    known: list = field(default_factory=list)  # (month, employee, reason)
    lead_months: int = 2


def forecast(db: Session, user: User, months: int = 12, turnover: Optional[float] = None, growth: float = 0.0,
             today: Optional[date] = None) -> Forecast:
    today = today or date.today()
    scope = user_scope(db, user)
    start = add_months(today.replace(day=1), 1)
    f = Forecast(months=[add_months(start, i) for i in range(months)])
    try:
        f.lead_months = int(get_setting(db, "hire_lead_months") or 2)
    except (TypeError, ValueError):
        pass
    active = [e for e in db.scalars(select(Employee).where(Employee.status == "Active")) if scope.allows_unit(e.org_unit_code)]
    dept = {e.employee_id: dept_of_unit(db, e.org_unit_code) for e in active}
    depts = sorted({d for d in dept.values() if d})
    # last 12 months' leavers per department, retirements and contract ends left out (they are counted from dates)
    leavers = defaultdict(int)
    for ev in db.scalars(select(EmploymentEvent).where(EmploymentEvent.event_type == "exit", EmploymentEvent.effective_date > today - timedelta(days=365))):
        e = db.get(Employee, ev.employee_id)
        if e and (e.exit_reason_code or "") not in ("RETIRE", "RETIRED", "CONTRACT", "CONTRACT-END") and scope.allows_unit(ev.from_org_unit or e.org_unit_code):
            leavers[dept_of_unit(db, ev.from_org_unit or e.org_unit_code)] += 1
    seats = defaultdict(int)
    for p in db.scalars(select(Position).where(Position.active.is_(True), Position.budgeted.is_(True), Position.frozen.is_(False))):
        if scope.allows_unit(p.org_unit_code):
            seats[dept_of_unit(db, p.org_unit_code)] += p.seats
    hires = defaultdict(lambda: defaultdict(int))
    for r in db.scalars(select(Requisition).where(Requisition.status == "open")):
        if scope.allows_unit(r.org_unit_code):
            m = max(r.needed_by.replace(day=1), start)
            hires[dept_of_unit(db, r.org_unit_code)][m] += r.open_seats if hasattr(r, "open_seats") else r.headcount
    known = defaultdict(lambda: defaultdict(int))
    for e in active:
        d = dept[e.employee_id]
        for when, why in ((retire_on(db, e), "Retires"), (e.contract_end_date, "Contract ends"), (e.exit_date, "Exit approved")):
            if when and start <= when.replace(day=1) <= f.months[-1]:
                known[d][when.replace(day=1)] += 1
                f.known.append((when, e, why))
                break
    f.known.sort(key=lambda x: x[0])
    for d in depts:
        hc = sum(1 for v in dept.values() if v == d)
        # leavers over the average headcount of the year (today + those who left), capped at 100%
        rate = turnover if turnover is not None else min(100.0, 100 * leavers[d] / (hc + leavers[d] / 2) if hc + leavers[d] else 0)
        budget = round(seats[d] * (1 + growth / 100))
        row = {"start": hc, "budget": budget, "rate": round(rate, 1), "projected": [], "known": [], "expected": [], "hires": [], "gap": []}
        cur = float(hc)
        for m in f.months:
            k, h = known[d][m], hires[d][m]
            x = cur * rate / 100 / 12
            cur = cur - k - x + h
            row["known"].append(k)
            row["expected"].append(round(x, 1))
            row["hires"].append(h)
            row["projected"].append(round(cur, 1))
            row["gap"].append(round(budget - cur, 1))
        f.rows[d] = row
    f.total = {"start": sum(r["start"] for r in f.rows.values()), "budget": sum(r["budget"] for r in f.rows.values()),
               "projected": [round(sum(r["projected"][i] for r in f.rows.values()), 1) for i in range(months)],
               "known": [sum(r["known"][i] for r in f.rows.values()) for i in range(months)],
               "expected": [round(sum(r["expected"][i] for r in f.rows.values()), 1) for i in range(months)],
               "hires": [sum(r["hires"][i] for r in f.rows.values()) for i in range(months)]}
    f.total["gap"] = [round(f.total["budget"] - p, 1) for p in f.total["projected"]]
    return f


def start_hiring(f: Forecast, d: str) -> Optional[tuple[date, int]]:
    """When to start hiring so people arrive before the gap opens (lead time from recruitment)."""
    row = f.rows[d]
    for i, g in enumerate(row["gap"]):
        if g >= 1:
            return add_months(f.months[i], -f.lead_months), int(g)
    return None
