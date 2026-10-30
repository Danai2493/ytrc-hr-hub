"""Skills matrix (ILUO) and training needs.

Each job lists the skills it needs and the level (I, L, U, O). Supervisors
record each person's level; the matrix shows gaps per person and, per skill,
whether enough people are at U or O to run every shift. Gaps, expiring
licences and induction become training needs; a passed course or an OJT
sign-off raises the level and closes the need."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Course, Employee, EmployeeSkill, JobSkill, Skill, TrainingNeed, TrainingRecord, User
from ..models.develop import LEVELS
from .common import audit, next_ref


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return date(y, m, 28)


def letter(level: Optional[int]) -> str:
    return LEVELS.get(level or 0, LEVELS[0])[0]


def required(db: Session, job_code: Optional[str]) -> dict[str, int]:
    if not job_code:
        return {}
    return {r.skill_code: r.level for r in db.scalars(select(JobSkill).where(JobSkill.job_code == job_code))}


def levels_of(db: Session, emp_ids: list[str]) -> dict[tuple[str, str], EmployeeSkill]:
    if not emp_ids:
        return {}
    rows = db.scalars(select(EmployeeSkill).where(EmployeeSkill.employee_id.in_(emp_ids))).all()
    return {(r.employee_id, r.skill_code): r for r in rows}


def effective_level(es: Optional[EmployeeSkill], today: date) -> int:
    """A lapsed licence or refresher counts as L: the person knows the job
    but may not do it alone until renewed."""
    if es is None:
        return 0
    if es.valid_until and es.valid_until < today and es.level > 2:
        return 2
    return es.level


@dataclass
class Matrix:
    people: list[Employee]
    skills: list[Skill]
    cells: dict[tuple[str, str], dict] = field(default_factory=dict)  # (emp, skill) -> {level, need, lapsed}
    coverage: dict[str, dict] = field(default_factory=dict)  # skill -> {uo, min, short}
    gaps: int = 0


def matrix(db: Session, people: list[Employee], today: Optional[date] = None) -> Matrix:
    today = today or date.today()
    req_by_job: dict[str, dict[str, int]] = {}
    for p in people:
        if p.job_code not in req_by_job:
            req_by_job[p.job_code] = required(db, p.job_code)
    units = {p.org_unit_code for p in people}
    codes = set()
    for r in req_by_job.values():
        codes |= set(r)
    held = levels_of(db, [p.employee_id for p in people])
    codes |= {k[1] for k, v in held.items() if v.level > 0}
    skills = [s for s in db.scalars(select(Skill).where(Skill.active.is_(True)).order_by(Skill.sort_order)) if s.code in codes or (s.org_unit_code in units and s.critical)]
    m = Matrix(people, skills)
    for s in skills:
        m.coverage[s.code] = {"uo": 0, "min": s.min_uo if (s.org_unit_code in units or s.org_unit_code is None) else 0}
    for p in people:
        req = req_by_job.get(p.job_code, {})
        for s in skills:
            es = held.get((p.employee_id, s.code))
            lvl = effective_level(es, today)
            need = req.get(s.code)
            lapsed = bool(es and es.valid_until and es.valid_until < today)
            m.cells[(p.employee_id, s.code)] = {"level": lvl, "need": need, "gap": bool(need and lvl < need), "lapsed": lapsed,
                                                "due": bool(es and es.valid_until and today <= es.valid_until <= today + timedelta(days=60))}
            if need and lvl < need:
                m.gaps += 1
            if lvl >= 3:
                m.coverage[s.code]["uo"] += 1
    for c in m.coverage.values():
        c["short"] = max(c["min"] - c["uo"], 0)
    return m


def can_assess(user: User) -> bool:
    return user.has("hr", "hr_manager", "dept_head", "supervisor")


def set_level(db: Session, user: User, emp: Employee, skill: Skill, level: int, note: str = "", on: Optional[date] = None,
              valid_until: Optional[date] = None) -> EmployeeSkill:
    if level not in LEVELS:
        raise ValueError("Level must be 0 to 4")
    on = on or date.today()
    es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == emp.employee_id, EmployeeSkill.skill_code == skill.code))
    before = es.level if es else 0
    if es is None:
        es = EmployeeSkill(employee_id=emp.employee_id, skill_code=skill.code, history=[])
        es.employee, es.skill = emp, skill
        db.add(es)
    es.level, es.assessed_on, es.assessed_by = level, on, user.label
    if valid_until is not None:
        es.valid_until = valid_until
    elif skill.valid_months and level >= 3 and level > before:
        es.valid_until = add_months(on, skill.valid_months)
    es.note = (note or "")[:300] or es.note
    es.history = [*(es.history or []), {"level": level, "on": on.isoformat(), "by": user.label, "note": (note or "")[:120]}][-20:]
    db.flush()
    audit(db, user, "skill", "employee", emp.employee_id, f"{skill.code}: {letter(before)} -> {letter(level)}")
    _close_met_needs(db, emp, skill.code, level)
    return es


def _close_met_needs(db: Session, emp: Employee, skill_code: str, level: int) -> None:
    for n in db.scalars(select(TrainingNeed).where(TrainingNeed.employee_id == emp.employee_id, TrainingNeed.skill_code == skill_code,
                                                   TrainingNeed.status.in_(("open", "planned")))):
        if n.reason in ("expiry",):
            continue  # closed by the renewal itself
        if level >= (n.target_level or 0):
            n.status, n.closed_at = "done", datetime.now()


# ---------- training ----------

def record_session(db: Session, user: User, course: Course, on: date, emp_ids: list[str], results: dict[str, str],
                   trainer: str = "", hours: Optional[float] = None, note: str = "", source: str = "web",
                   capture_id: Optional[int] = None) -> tuple[str, list[TrainingRecord]]:
    """One class, many people. A pass on a course linked to a skill raises the
    person's level to the course level (never lowers it) and renews licences."""
    ref = next_ref(db, "TR", on.year)
    made = []
    for eid in emp_ids:
        emp = db.get(Employee, eid)
        if emp is None:
            continue
        res = results.get(eid, "passed" if course.skill_code else "attended")
        if res not in ("attended", "passed", "failed"):
            res = "attended"
        valid = add_months(on, course.valid_months) if course.valid_months and res == "passed" else None
        r = TrainingRecord(employee_id=eid, course_code=course.code, session_ref=ref, on_date=on, hours=hours if hours is not None else course.hours,
                           result=res, trainer=trainer[:120] or None, valid_until=valid, note=note[:300] or None, source=source,
                           capture_id=capture_id, recorded_by=user.label)
        r.employee, r.course = emp, course
        db.add(r)
        made.append(r)
        if res == "passed" and course.skill_code:
            skill = db.get(Skill, course.skill_code)
            es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == eid, EmployeeSkill.skill_code == course.skill_code))
            cur = es.level if es else 0
            target = max(cur, course.skill_level or cur)
            if target != cur or valid:
                set_level(db, user, emp, skill, target, f"{course.code} passed ({ref})", on, valid_until=valid)
        if res in ("passed", "attended") and (res == "passed" or not course.skill_code):
            for n in db.scalars(select(TrainingNeed).where(TrainingNeed.employee_id == eid, TrainingNeed.status.in_(("open", "planned")))):
                if n.course_code == course.code or (n.reason == "expiry" and n.skill_code and n.skill_code == course.skill_code):
                    n.status, n.closed_at = "done", datetime.now()
    db.flush()
    audit(db, user, "training", "course", course.code, f"{ref}: {len(made)} people on {on}")
    return ref, made


def generate_needs(db: Session, today: Optional[date] = None, emp_ids: Optional[set[str]] = None) -> int:
    """Turn gaps, lapsing licences and missing induction into training needs.
    Runs with the nightly checks; existing open needs are not duplicated."""
    today = today or date.today()
    people = [e for e in db.scalars(select(Employee).where(Employee.status == "Active")) if emp_ids is None or e.employee_id in emp_ids]
    open_needs = {(n.employee_id, n.skill_code, n.course_code, n.reason) for n in db.scalars(select(TrainingNeed).where(TrainingNeed.status.in_(("open", "planned"))))}
    done = {(r.employee_id, r.course_code) for r in db.scalars(select(TrainingRecord).where(TrainingRecord.result.in_(("attended", "passed"))))}
    skills = {s.code: s for s in db.scalars(select(Skill))}
    induction = list(db.scalars(select(Course).where(Course.for_new_staff.is_(True), Course.active.is_(True))))
    held = levels_of(db, [p.employee_id for p in people])
    added = 0

    def add(e, reason, skill=None, course=None, cur=None, target=None, prio=2, due=None):
        nonlocal added
        key = (e.employee_id, skill, course, reason)
        if key in open_needs:
            return
        open_needs.add(key)
        db.add(TrainingNeed(employee_id=e.employee_id, skill_code=skill, course_code=course, reason=reason, current_level=cur,
                            target_level=target, priority=prio, due_date=due))
        added += 1

    for e in people:
        for c in induction:
            if (e.employee_id, c.code) not in done and (today - e.join_date).days <= 365:
                add(e, "new_staff", c.skill_code, c.code, prio=1, due=e.join_date + timedelta(days=7))
        for code, lvl in required(db, e.job_code).items():
            s = skills.get(code)
            es = held.get((e.employee_id, code))
            cur = effective_level(es, today)
            if s is None:
                continue
            if es and es.valid_until and es.valid_until <= today + timedelta(days=60) and es.level >= 3:
                add(e, "expiry", code, s.course_code, cur, lvl, 1, es.valid_until)
            elif cur < lvl:
                legal = s.category in ("Safety", "Legal licence")
                add(e, "legal" if legal else "gap", code, s.course_code, cur, lvl, 1 if (legal or s.critical) else 2,
                    today + timedelta(days=30 if s.critical else 90))
    db.flush()
    return added


def training_hours(db: Session, emp_ids: Optional[set[str]], d0: date, d1: date) -> float:
    rows = db.scalars(select(TrainingRecord).where(TrainingRecord.on_date >= d0, TrainingRecord.on_date <= d1)).all()
    return round(sum(r.hours for r in rows if emp_ids is None or r.employee_id in emp_ids), 1)
