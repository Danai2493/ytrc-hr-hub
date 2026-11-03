"""Annual training plan (from needs, legal refreshers and development plans), cost, and
whether training worked: L1 (how useful it felt), L2 (pass / skill level gained),
L3 (supervisor sees it used on the job 1-3 months later). Simple cost per hour, no ROI theatre."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Course, EmployeeSkill, Skill, TrainingNeed, TrainingPlanItem, TrainingRecord, User
from ..models.develop import L3_RESULTS, PLAN_SOURCES
from .common import audit

HR = ("hr", "hr_manager")
L3_AFTER_DAYS = 30  # ask the supervisor from one month after the class
L3_LATE_DAYS = 90


class PlanError(ValueError):
    pass


def _cost(c: Course, n: int) -> Optional[float]:
    return round(c.cost_per_person * n, 2) if c.cost_per_person else None


def draft(db: Session, user: User, year: int, today: Optional[date] = None) -> int:
    """Add plan lines for courses not yet in the plan: open needs (by due month) and licences
    that lapse during the year. HR then moves months and numbers by hand."""
    if not user.has(*HR):
        raise PlanError("Only HR builds the training plan")
    today = today or date.today()
    have = {(i.course_code, i.month) for i in db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == year, TrainingPlanItem.status != "cancelled"))}
    have_courses = {c for c, _ in have}
    want: dict[str, dict] = defaultdict(lambda: {"months": [], "people": set(), "source": "needs"})
    by_skill = {c.skill_code: c for c in db.scalars(select(Course).where(Course.active.is_(True), Course.skill_code.is_not(None)))}
    for n in db.scalars(select(TrainingNeed).where(TrainingNeed.status.in_(("open", "planned")))):
        c = n.course or (by_skill.get(n.skill_code) if n.skill_code else None)
        if c is None:
            continue
        due = n.due_date or today + timedelta(days=90)
        m = due.month if due.year == year else (1 if due.year < year else 12)
        w = want[c.code]
        w["months"].append(m)
        w["people"].add(n.employee_id)
        if n.reason == "idp":
            w["source"] = "idp"
        elif n.reason == "expiry":
            w["source"] = "legal"
    for es in db.scalars(select(EmployeeSkill).where(EmployeeSkill.valid_until >= date(year, 1, 1), EmployeeSkill.valid_until <= date(year, 12, 31))):
        c = by_skill.get(es.skill_code)
        if c and es.employee.status == "Active":
            w = want[c.code]
            w["months"].append(max(1, es.valid_until.month - 1))
            w["people"].add(es.employee_id)
            w["source"] = "legal"
    added = 0
    for code, w in want.items():
        if code in have_courses or not w["people"]:
            continue
        c = db.get(Course, code)
        month = min(w["months"])
        if year == today.year and month < today.month:
            month = today.month
        n = len(w["people"])
        db.add(TrainingPlanItem(year=year, month=month, course_code=code, people=n, budget=_cost(c, n), source=w["source"], created_by=user.label))
        added += 1
    if added:
        audit(db, user, "training_plan", "training_plan", str(year), f"{added} lines drafted")
    db.flush()
    return added


def save_item(db: Session, user: User, year: int, form, item: Optional[TrainingPlanItem] = None) -> TrainingPlanItem:
    if not user.has(*HR):
        raise PlanError("Only HR changes the training plan")
    c = db.get(Course, form.get("course") or (item.course_code if item else ""))
    if c is None:
        raise PlanError("Choose a course")
    try:
        month = int(form.get("month") or 0)
        people = int(form.get("people") or 0)
        budget = float(form["budget"]) if (form.get("budget") or "").strip() else None
    except ValueError as e:
        raise PlanError("Month, people and budget must be numbers") from e
    if not 1 <= month <= 12 or people < 1:
        raise PlanError("Month 1-12 and at least one person")
    if item is None:
        item = TrainingPlanItem(year=year, course_code=c.code, source="manual", created_by=user.label)
        db.add(item)
    item.month, item.people = month, people
    item.budget = budget if budget is not None else _cost(c, people)
    item.org_unit_code = (form.get("unit") or "").strip() or None
    item.note = (form.get("note") or "").strip()[:300] or None
    st = form.get("status")
    if st in ("planned", "done", "cancelled"):
        item.status = st
    db.flush()
    return item


@dataclass
class PlanRow:
    course: Course
    items: list = field(default_factory=list)
    planned_people: int = 0
    budget: float = 0
    done_people: int = 0
    hours: float = 0
    cost: float = 0
    months: list = field(default_factory=list)


def plan_vs_actual(db: Session, year: int) -> tuple[list[PlanRow], dict]:
    rows: dict[str, PlanRow] = {}
    for i in db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == year, TrainingPlanItem.status != "cancelled").order_by(TrainingPlanItem.month)):
        r = rows.setdefault(i.course_code, PlanRow(i.course))
        r.items.append(i)
        r.planned_people += i.people
        r.budget += i.budget or 0
        r.months.append(i.month)
    for t in db.scalars(select(TrainingRecord).where(TrainingRecord.on_date >= date(year, 1, 1), TrainingRecord.on_date <= date(year, 12, 31))):
        r = rows.setdefault(t.course_code, PlanRow(t.course))
        r.done_people += 1
        r.hours += t.hours or 0
        r.cost += t.cost or 0
    out = sorted(rows.values(), key=lambda r: (min(r.months) if r.months else 13, r.course.code))
    by_month = [{"planned": 0, "done": 0} for _ in range(12)]
    for i in db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == year, TrainingPlanItem.status != "cancelled")):
        by_month[i.month - 1]["planned"] += i.people
    for t in db.scalars(select(TrainingRecord).where(TrainingRecord.on_date >= date(year, 1, 1), TrainingRecord.on_date <= date(year, 12, 31))):
        by_month[t.on_date.month - 1]["done"] += 1
    tot = {"planned": sum(r.planned_people for r in out), "budget": sum(r.budget for r in out), "done": sum(r.done_people for r in out),
           "hours": sum(r.hours for r in out), "cost": sum(r.cost for r in out), "unplanned": sum(1 for r in out if not r.items and r.done_people),
           "by_month": by_month}
    return out, tot


# ---------------- effectiveness ----------------

def session(db: Session, ref: str) -> list[TrainingRecord]:
    return list(db.scalars(select(TrainingRecord).where(TrainingRecord.session_ref == ref).order_by(TrainingRecord.employee_id)))


def save_session_eval(db: Session, user: User, ref: str, form) -> None:
    """L1 average from the class feedback sheet, and the session's total cost shared per person."""
    if not user.has(*HR):
        raise PlanError("Only HR records class results")
    recs = session(db, ref)
    if not recs:
        raise PlanError("Class not found")
    try:
        l1 = float(form["l1"]) if (form.get("l1") or "").strip() else None
        cost = float(form["cost"]) if (form.get("cost") or "").strip() else None
    except ValueError as e:
        raise PlanError("Feedback score and cost must be numbers") from e
    if l1 is not None and not 1 <= l1 <= 5:
        raise PlanError("Feedback score is 1 to 5")
    if cost is not None and cost < 0:
        raise PlanError("Cost cannot be negative")
    for r in recs:
        if l1 is not None:
            r.l1_score = l1
        if cost is not None:
            r.cost = round(cost / len(recs), 2)
    audit(db, user, "training_eval", "training", ref, f"L1 {l1}, cost {cost}")


def followups(db: Session, team: set[str], today: Optional[date] = None) -> list[TrainingRecord]:
    today = today or date.today()
    q = select(TrainingRecord).where(TrainingRecord.l3_result.is_(None), TrainingRecord.result != "failed",
                                     TrainingRecord.on_date <= today - timedelta(days=L3_AFTER_DAYS),
                                     TrainingRecord.on_date >= today - timedelta(days=270)).order_by(TrainingRecord.on_date)
    return [r for r in db.scalars(q) if r.employee_id in team and r.employee.status == "Active"]


def save_followup(db: Session, user: User, rec: TrainingRecord, result: str, note: str) -> None:
    if result not in L3_RESULTS:
        raise PlanError("Choose a result")
    if user.employee_id == rec.employee_id:
        raise PlanError("Your supervisor answers this for you")
    if result != "applied" and len((note or "").strip()) < 5:
        raise PlanError("Say what is missing, so the next step is clear")
    rec.l3_result, rec.l3_note, rec.l3_by, rec.l3_on = result, (note or "").strip()[:300] or None, user.label, date.today()
    audit(db, user, "training_l3", "employee", rec.employee_id, f"{rec.course_code} {rec.session_ref}: {result}")


@dataclass
class Effect:
    course: Course
    people: int = 0
    hours: float = 0
    cost: float = 0
    passed: int = 0
    graded: int = 0
    l1: list = field(default_factory=list)
    l3: dict = field(default_factory=lambda: {"applied": 0, "partly": 0, "not_yet": 0})
    gains: list = field(default_factory=list)

    @property
    def cost_per_hour(self) -> Optional[float]:
        return round(self.cost / self.hours) if self.cost and self.hours else None

    @property
    def l1_avg(self) -> Optional[float]:
        return round(sum(self.l1) / len(self.l1), 1) if self.l1 else None

    @property
    def pass_pct(self) -> Optional[int]:
        return round(100 * self.passed / self.graded) if self.graded else None

    @property
    def l3_answered(self) -> int:
        return sum(self.l3.values())

    @property
    def applied_pct(self) -> Optional[int]:
        n = self.l3_answered
        return round(100 * (self.l3["applied"] + 0.5 * self.l3["partly"]) / n) if n else None

    @property
    def gain(self) -> Optional[float]:
        return round(sum(self.gains) / len(self.gains), 1) if self.gains else None


def effectiveness(db: Session, d0: date, d1: date, team: Optional[set[str]] = None) -> list[Effect]:
    out: dict[str, Effect] = {}
    levels = {}
    for t in db.scalars(select(TrainingRecord).where(TrainingRecord.on_date >= d0, TrainingRecord.on_date <= d1)):
        if team is not None and t.employee_id not in team:
            continue
        e = out.setdefault(t.course_code, Effect(t.course))
        e.people += 1
        e.hours += t.hours or 0
        e.cost += t.cost or 0
        if t.course.skill_code or t.course.legal:
            e.graded += 1
            e.passed += t.result == "passed"
        if t.l1_score:
            e.l1.append(t.l1_score)
        if t.l3_result:
            e.l3[t.l3_result] += 1
        if t.course.skill_code and t.level_before is not None and t.result == "passed":
            key = (t.employee_id, t.course.skill_code)
            if key not in levels:
                es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == t.employee_id, EmployeeSkill.skill_code == t.course.skill_code))
                levels[key] = es.level if es else 0
            e.gains.append(levels[key] - t.level_before)
    return sorted(out.values(), key=lambda e: (-e.people, e.course.code))
