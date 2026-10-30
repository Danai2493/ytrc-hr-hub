"""Performance appraisal and KPI (blueprint M10).

The year: HR opens a cycle -> company and unit goals -> each person agrees 3 to 7
KPIs with the supervisor (staff) or gets the operator form -> mid-year check-in ->
self review -> supervisor review (the app fills in attendance, lateness, skills
and unit goal results) -> Department Head endorses -> calibration by unit ->
HR Manager releases -> the employee reads and acknowledges.

A rating of D or E opens an improvement plan in Growth. The pay review export
carries the rating and the guideline merit % only; salaries stay in Bplus."""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Appraisal,
    DailyAttendance,
    DevelopmentPlan,
    Employee,
    Goal,
    KpiDefinition,
    OrgUnit,
    PerformanceCycle,
    User,
    UserScope,
)
from ..models.perform import DIRECTIONS, OPERATOR_PARTS, RATINGS
from .approvals import resolve_head
from .common import audit, get_setting, user_scope

HR = ("hr", "hr_manager")
DEFAULT_WEIGHTS = {"staff": {"kpi": 70, "comp": 30}, "manager": {"kpi": 60, "comp": 40},
                   "operator": {"team": 20, "attendance": 25, "skills": 25, "behaviour": 30}}
DEFAULT_GUIDE = {"5": 10, "4": 20, "3": 55, "2": 10, "1": 5}  # % of people, a guide for calibration, never a quota
DEFAULT_MERIT = {"5": 6.0, "4": 4.5, "3": 3.0, "2": 1.0, "1": 0.0}
BANDS = (120, 105, 95, 80)  # achievement % for scores 5, 4, 3, 2
STAFF_COMPS = ["SE1", "DE1", "DE2", "PE1", "PE2", "TH1", "TH3"]
LEADER_COMPS = ["LE1", "LE2", "LE3", "LE4"]
OPERATOR_COMPS = ["SE1", "DE1", "DE2", "PE2", "TH3"]
MIN_SERVICE_DAYS = 90


class PerformError(Exception):
    pass


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


def letter(r: Optional[int]) -> str:
    return RATINGS[r][0] if r in RATINGS else "-"


# ---------- scoring ----------

def achievement(direction: str, target: Optional[float], actual: Optional[float]) -> Optional[float]:
    if target is None or actual is None:
        return None
    if direction == "down":
        if actual <= 0:
            return 150.0 if target >= 0 else None
        return round(100 * target / actual, 1) if target > 0 else (150.0 if actual <= target else 50.0)
    if direction == "exact":
        if target == 0:
            return 100.0 if actual == 0 else 50.0
        return round(max(0.0, 100 - abs(actual - target) / abs(target) * 100), 1)
    if target == 0:
        return 150.0 if actual >= 0 else 50.0
    return round(100 * actual / target, 1)


def score_from(pct: Optional[float]) -> Optional[int]:
    if pct is None:
        return None
    for s, b in zip((5, 4, 3, 2), BANDS):
        if pct >= b:
            return s
    return 1


def goal_score(g: Goal) -> tuple[Optional[float], Optional[int]]:
    direction = g.kpi.direction if g.kpi else "up"
    pct = achievement(direction, g.target, g.actual)
    return pct, score_from(pct)


# ---------- people ----------

def form_for(emp: Employee) -> str:
    return "operator" if emp.employee_group == "Operator" else "staff"


def is_leader(emp: Employee) -> bool:
    from .growth import LEADER_GRADES, LEADER_JOBS

    return emp.employee_group == "Manager" or emp.job_code in LEADER_JOBS or (emp.grade_code or "") in LEADER_GRADES


def comps_for(emp: Employee) -> list[str]:
    if form_for(emp) == "operator":
        return list(OPERATOR_COMPS)
    return STAFF_COMPS + (LEADER_COMPS if is_leader(emp) else [])


def reviewers(db: Session, emp: Employee) -> tuple[Optional[str], Optional[str]]:
    """Direct supervisor (a supervisor who covers the unit, else the unit head up the tree)
    and the Department Head one level above, who endorses."""
    sup = None
    for us in db.scalars(select(UserScope).where(UserScope.org_unit_code == emp.org_unit_code)):
        u = db.get(User, us.user_id)
        if u and u.active and u.employee_id and u.employee_id != emp.employee_id and u.has("supervisor"):
            sup = u.employee_id
            break
    if sup is None:
        sup = resolve_head(db, emp.org_unit_code, None)
        if sup == emp.employee_id:
            unit = db.get(OrgUnit, emp.org_unit_code)
            sup = resolve_head(db, unit.parent_code, None) if unit and unit.parent_code else None
    if sup is None:  # a Department Head with no head above: the GM reviews
        gm = next((u.employee_id for u in db.scalars(select(User).where(User.active.is_(True), User.employee_id.is_not(None)))
                   if u.has("gm") and u.employee_id != emp.employee_id), None)
        sup = gm
    endorser = resolve_head(db, emp.org_unit_code, None)
    if endorser in (sup, emp.employee_id):
        unit = db.get(OrgUnit, emp.org_unit_code)
        code = unit.parent_code if unit else None
        endorser = None
        while code:
            h = resolve_head(db, code, None)
            if h not in (sup, emp.employee_id, None):
                endorser = h
                break
            u = db.get(OrgUnit, code)
            code = u.parent_code if u else None
    return sup, endorser


# ---------- cycle ----------

def open_cycle(db: Session, user: User, name: str, start: date, end: date, goals_due: date, midyear_on: Optional[date], review_due: date,
               groups: list[str]) -> tuple[PerformanceCycle, list[str]]:
    if end <= start:
        raise PerformError("The year must end after it starts")
    if not (start <= goals_due <= end):
        raise PerformError("Goals are due inside the year")
    if not groups:
        raise PerformError("Choose at least one employee group")
    c = PerformanceCycle(name=name[:60], period_start=start, period_end=end, goals_due=goals_due, midyear_on=midyear_on, review_due=review_due,
                         groups=groups, weights=DEFAULT_WEIGHTS, guide=DEFAULT_GUIDE, merit_guide=DEFAULT_MERIT, status="goals")
    db.add(c)
    db.flush()
    skipped = []
    for e in db.scalars(select(Employee).where(Employee.status == "Active", Employee.employee_group.in_(groups)).order_by(Employee.employee_id)):
        if e.join_date > end - timedelta(days=MIN_SERVICE_DAYS):
            skipped.append(e.employee_id)  # too new to rate this year; probation covers them
            continue
        add_person(db, c, e)
    audit(db, user, "create", "performance_cycle", c.id, f"{c.name}: {len(groups)} group(s), {len(skipped)} too new to rate")
    return c, skipped


def add_person(db: Session, c: PerformanceCycle, e: Employee) -> Appraisal:
    sup, endorser = reviewers(db, e)
    form = form_for(e)
    a = Appraisal(cycle_id=c.id, employee_id=e.employee_id, form=form, status="goals", reviewer_employee_id=sup, endorser_employee_id=endorser,
                  org_unit_code=e.org_unit_code, items=[], competencies={k: {} for k in comps_for(e)},
                  parts={k: {} for k in OPERATOR_PARTS} if form == "operator" else {})
    a.employee, a.cycle = e, c
    if form == "operator":
        a.status = "goals_agreed"  # operators share the unit goals; nothing to set one by one
    db.add(a)
    db.flush()
    return a


def advance_cycle(db: Session, user: User, c: PerformanceCycle, status: str) -> None:
    order = ["goals", "midyear", "review", "calibration"]
    if status not in order or order.index(status) <= order.index(c.status if c.status in order else "calibration"):
        raise PerformError("The cycle only moves forward")
    c.status = status
    audit(db, user, "status", "performance_cycle", c.id, status)


# ---------- goals ----------

def save_goal(db: Session, user: User, c: PerformanceCycle, data: dict, goal: Optional[Goal] = None) -> Goal:
    level = data.get("level") or "unit"
    unit = data.get("org_unit_code") or None
    if level == "company" and not user.has("hr_manager", "gm"):
        raise PerformError("Company goals are set by the GM or the HR Manager")
    if level == "unit":
        if not unit:
            raise PerformError("Choose the unit")
        scope = user_scope(db, user)
        if not (scope.all or unit in scope.org_units):
            raise PerformError("You can set goals only for your own units")
    title = (data.get("title") or "").strip()
    kpi = db.get(KpiDefinition, data.get("kpi_code")) if data.get("kpi_code") else None
    if not title and kpi:
        title = kpi.name_en
    if not title:
        raise PerformError("Goal title is required")
    target = _num(data.get("target"))
    if target is None:
        raise PerformError("Every goal needs a number target")
    g = goal or Goal(cycle_id=c.id, level=level)
    if goal is None:
        db.add(g)
    g.level, g.org_unit_code = level, unit if level == "unit" else None
    g.kpi_code, g.title, g.target = kpi.code if kpi else None, title[:200], target
    g.baseline = _num(data.get("baseline"))
    g.weight = int(_num(data.get("weight")) or 0)
    g.parent_id = int(data["parent_id"]) if data.get("parent_id") else None
    g.owner = data.get("owner") or user.label
    if "actual" in data and data.get("actual") not in (None, ""):
        g.actual, g.actual_on = _num(data.get("actual")), date.today()
    db.flush()
    audit(db, user, "save", "goal", g.id, f"{g.level} {g.org_unit_code or ''} {g.title}: target {g.target}")
    return g


def unit_goals(db: Session, c: PerformanceCycle, unit_code: str) -> list[Goal]:
    """Goals of the unit and every unit above it (a section shares its department's goals)."""
    out, code, seen = [], unit_code, set()
    while code and code not in seen:
        seen.add(code)
        out += list(db.scalars(select(Goal).where(Goal.cycle_id == c.id, Goal.level == "unit", Goal.org_unit_code == code)))
        u = db.get(OrgUnit, code)
        code = u.parent_code if u else None
    return out


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


# ---------- KPI items ----------

ITEM_FIELDS = ("title", "kpi_code", "goal_id", "measure", "unit", "direction", "target", "weight")


def parse_items(form, existing: list[dict]) -> list[dict]:
    """Rows t0..t7 from the goal form. Unknown rows are dropped; ids keep comments and scores attached."""
    by_id = {i.get("id"): i for i in existing}
    out = []
    for n in range(8):
        title = (form.get(f"t{n}") or "").strip()
        if not title:
            continue
        iid = form.get(f"id{n}") or uuid.uuid4().hex[:8]
        it = dict(by_id.get(iid, {}))
        it.update(id=iid, title=title[:200], kpi_code=form.get(f"k{n}") or None, goal_id=int(form.get(f"g{n}")) if form.get(f"g{n}") else None,
                  measure=(form.get(f"m{n}") or "")[:300] or None, unit=(form.get(f"u{n}") or "")[:20] or None,
                  direction=form.get(f"d{n}") if form.get(f"d{n}") in DIRECTIONS else "up", target=_num(form.get(f"tg{n}")),
                  weight=int(_num(form.get(f"w{n}")) or 0))
        out.append(it)
    return out


def item_checks(items: list[dict]) -> list[dict]:
    out = []
    total = sum(i.get("weight") or 0 for i in items)
    if not items:
        return [finding("block", "kpi_none")]
    if total != 100:
        out.append(finding("block", "kpi_weights", total=total))
    if not 3 <= len(items) <= 7:
        out.append(finding("flag", "kpi_count", n=len(items)))
    for i in items:
        if i.get("target") is None:
            out.append(finding("block", "kpi_no_target", title=i["title"]))
        w = i.get("weight") or 0
        if w and (w < 5 or w > 40):
            out.append(finding("flag", "kpi_weight_range", title=i["title"], w=w))
    if not any(i.get("goal_id") for i in items):
        out.append(finding("info", "kpi_no_link"))
    titles = Counter(i["title"].lower() for i in items)
    for t, n in titles.items():
        if n > 1:
            out.append(finding("flag", "kpi_duplicate", title=t))
    return out


def can_view(db: Session, user: User, a: Appraisal) -> bool:
    if user.employee_id and user.employee_id in (a.employee_id, a.reviewer_employee_id, a.endorser_employee_id):
        return True
    if user.has("hr", "hr_manager", "gm", "auditor"):
        return True
    if user.has("dept_head", "supervisor"):
        scope = user_scope(db, user)
        return a.org_unit_code in scope.org_units
    return False


def is_reviewer(user: User, a: Appraisal) -> bool:
    return bool(user.employee_id) and user.employee_id == a.reviewer_employee_id


def _not_own(user: User, a: Appraisal) -> None:
    if user.employee_id and user.employee_id == a.employee_id:
        raise PerformError("You cannot approve or rate your own appraisal")


def can_set_goals(user: User, a: Appraisal) -> bool:
    if a.form != "staff" or a.status not in ("goals", "goals_agreed"):
        return False
    if a.cycle.status not in ("goals", "midyear") and not (a.status == "goals" and a.cycle.status == "review"):
        return False  # late goals can still be agreed while the reviews are open
    return is_reviewer(user, a) or user.has(*HR) or (user.employee_id == a.employee_id and a.status == "goals")


def save_goals(db: Session, user: User, a: Appraisal, form, agree: bool = False) -> list[dict]:
    if not can_set_goals(user, a):
        raise PerformError("Goals cannot be changed now")
    items = parse_items(form, a.items or [])
    if a.status == "goals_agreed" and not (form.get("change_note") or "").strip():
        raise PerformError("Goals were agreed: write why they change")
    a.items = items
    checks = item_checks(items)
    if agree:
        _not_own(user, a)
        if not (is_reviewer(user, a) or user.has(*HR)):
            raise PerformError("The supervisor agrees the goals")
        if any(c["level"] == "block" for c in checks):
            raise PerformError("Fix the goals first: " + "; ".join(rule_en(c) for c in checks if c["level"] == "block"))
        a.status, a.goals_agreed_at = "goals_agreed", datetime.now()
    audit(db, user, "goals", "appraisal", a.id, f"{len(items)} KPI(s){' agreed' if agree else ''}. {form.get('change_note') or ''}"[:300])
    return checks


def midyear(db: Session, user: User, a: Appraisal, note: str, progress: dict) -> None:
    if a.cycle.status not in ("midyear", "review"):
        raise PerformError("The mid-year check-in opens in the middle of the year")
    if not (is_reviewer(user, a) or user.has(*HR) or user.employee_id == a.employee_id):
        raise PerformError("Not your appraisal")
    items = [dict(i) for i in a.items or []]
    for i in items:
        v = progress.get(i["id"])
        if v not in (None, ""):
            i["mid"] = str(v)[:200]
    a.items = items
    a.midyear_note = (note or "")[:2000] or None
    a.midyear_at = datetime.now()
    audit(db, user, "midyear", "appraisal", a.id, "Mid-year check-in")


# ---------- facts the app fills in ----------

def collect_facts(db: Session, a: Appraisal, today: Optional[date] = None) -> dict:
    from .skills import matrix, training_hours

    c = a.cycle
    today = today or date.today()
    d1 = min(c.period_end, today)
    emp = a.employee
    d0 = max(c.period_start, emp.join_date)
    rows = list(db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == emp.employee_id, DailyAttendance.work_date >= d0,
                                                         DailyAttendance.work_date <= d1)))
    codes = Counter(r.day_code for r in rows if not r.is_rest_day)
    planned = sum(n for k, n in codes.items() if k not in ("h", None))
    late = sum(1 for r in rows if (r.late_minutes or 0) > 0 and r.day_code == "1")
    m = matrix(db, [emp], today)
    req = [s for s in m.skills if m.cells[(emp.employee_id, s.code)]["need"]]
    met = [s for s in req if not m.cells[(emp.employee_id, s.code)]["gap"]]
    facts = {
        "from": d0.isoformat(), "to": d1.isoformat(), "planned_days": planned, "worked": codes.get("1", 0), "absent": codes.get("a", 0),
        "unpaid": codes.get("u", 0), "sick": codes.get("s", 0), "late": late,
        "attendance_pct": round(100 * codes.get("1", 0) / planned, 1) if planned else None,
        "skills_required": len(req), "skills_met": len(met), "skills_gaps": [s.name_en for s in req if s not in met][:6],
        "training_hours": training_hours(db, {emp.employee_id}, d0, d1),
    }
    a.facts = {**(a.facts or {}), **facts}
    return facts


def suggest_parts(db: Session, a: Appraisal) -> dict[str, Optional[int]]:
    """Operator form: scores the app proposes from the facts. The supervisor can change any of them with a note."""
    f = a.facts or {}
    team_scores = []
    for g in unit_goals(db, a.cycle, a.org_unit_code):
        _, s = goal_score(g)
        if s:
            team_scores += [s] * max(g.weight or 1, 1)
    team = round(sum(team_scores) / len(team_scores)) if team_scores else None
    absent, late = f.get("absent", 0) + f.get("unpaid", 0), f.get("late", 0)
    attendance = 5 if absent == 0 and late <= 2 else 4 if absent <= 1 and late <= 5 else 3 if absent <= 3 and late <= 10 else 2 if absent <= 6 else 1
    if not f.get("planned_days"):
        attendance = None
    req, met = f.get("skills_required", 0), f.get("skills_met", 0)
    skills = None
    if req:
        share = met / req
        skills = 5 if share >= 1 and f.get("training_hours", 0) >= 8 else 4 if share >= 1 else 3 if share >= .75 else 2 if share >= .5 else 1
    mgr = [v.get("mgr") for v in (a.competencies or {}).values() if v.get("mgr")]
    behaviour = round(sum(mgr) / len(mgr)) if mgr else None
    return {"team": team, "attendance": attendance, "skills": skills, "behaviour": behaviour}


# ---------- reviews ----------

def _int(v) -> Optional[int]:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 5 else None


def self_review(db: Session, user: User, a: Appraisal, form, submit: bool) -> None:
    if user.employee_id != a.employee_id:
        raise PerformError("Only the employee writes the self review")
    if a.cycle.status not in ("review", "calibration") or a.status not in ("goals_agreed", "self"):
        raise PerformError("Self review is not open")
    items = [dict(i) for i in a.items or []]
    for i in items:
        if f"a_{i['id']}" in form:
            i["actual"] = _num(form.get(f"a_{i['id']}"))
        i["self"] = _int(form.get(f"s_{i['id']}")) or i.get("self")
        i["self_comment"] = (form.get(f"c_{i['id']}") or i.get("self_comment") or "")[:500] or None
    a.items = items
    comps = {k: dict(v) for k, v in (a.competencies or {}).items()}
    for k in comps:
        s = _int(form.get(f"cs_{k}"))
        if s:
            comps[k]["self"] = s
    a.competencies = comps
    a.self_note = (form.get("self_note") or a.self_note or "")[:3000] or None
    a.status = "self"
    if submit:
        if a.form == "staff" and any(i.get("self") is None for i in items):
            raise PerformError("Score every KPI before you send")
        a.self_at, a.status = datetime.now(), "manager"
    audit(db, user, "self_review", "appraisal", a.id, "sent" if submit else "saved")


def skip_self(db: Session, user: User, a: Appraisal, reason: str) -> None:
    """Operators without a login: the supervisor talks it through and goes straight to the review."""
    _not_own(user, a)
    if not (is_reviewer(user, a) or user.has(*HR)):
        raise PerformError("Only the supervisor or HR")
    if a.cycle.status not in ("review", "calibration") or a.status not in ("goals_agreed", "self"):
        raise PerformError("Review is not open")
    a.self_note = f"[{reason or 'Self review done in the meeting'}]"
    a.status = "manager"
    audit(db, user, "skip_self", "appraisal", a.id, reason[:200])


def manager_review(db: Session, user: User, a: Appraisal, form, submit: bool) -> None:
    _not_own(user, a)
    if not (is_reviewer(user, a) or user.has("hr_manager")):
        raise PerformError("Only the direct supervisor reviews")
    if a.cycle.status not in ("review", "calibration") or a.status not in ("goals_agreed", "self", "manager"):
        raise PerformError("Supervisor review is not open")
    if a.status != "manager" and a.form == "staff" and submit:
        raise PerformError("Wait for the self review, or record that it was done in the meeting")
    items = [dict(i) for i in a.items or []]
    for i in items:
        if form.get(f"a_{i['id']}") not in (None, ""):
            i["actual"] = _num(form.get(f"a_{i['id']}"))
        auto = score_from(achievement(i.get("direction") or "up", i.get("target"), i.get("actual")))
        i["auto"] = auto
        i["mgr"] = _int(form.get(f"r_{i['id']}")) or i.get("mgr") or auto
        i["mgr_comment"] = (form.get(f"rc_{i['id']}") or i.get("mgr_comment") or "")[:500] or None
    a.items = items
    comps = {k: dict(v) for k, v in (a.competencies or {}).items()}
    for k in comps:
        s = _int(form.get(f"cm_{k}"))
        if s:
            comps[k]["mgr"] = s
        if form.get(f"cn_{k}") is not None:
            comps[k]["note"] = (form.get(f"cn_{k}") or "")[:300] or None
    a.competencies = comps
    collect_facts(db, a)
    if a.form == "operator":
        sug = suggest_parts(db, a)
        parts = {k: dict(v) for k, v in (a.parts or {}).items()}
        for k in OPERATOR_PARTS:
            p = parts.setdefault(k, {})
            p["suggested"] = sug.get(k)
            s = _int(form.get(f"p_{k}"))
            p["score"] = s or p.get("score") or sug.get(k)
            if form.get(f"pn_{k}") is not None:
                p["note"] = (form.get(f"pn_{k}") or "")[:300] or None
        a.parts = parts
    a.mgr_note = (form.get("mgr_note") or a.mgr_note or "")[:3000] or None
    compute(a)
    if submit:
        missing = []
        if a.form == "staff":
            missing += [i["title"] for i in items if i.get("mgr") is None]
        else:
            missing += [k for k, p in a.parts.items() if not p.get("score")]
        missing += [k for k, v in a.competencies.items() if not v.get("mgr")]
        if missing:
            raise PerformError("Score every line before you send: " + ", ".join(missing[:5]))
        if a.form == "operator":
            for k, p in a.parts.items():
                if p.get("suggested") and p["score"] != p["suggested"] and not p.get("note"):
                    raise PerformError(f"Explain why {OPERATOR_PARTS[k].lower()} differs from what the facts suggest")
        a.mgr_at, a.mgr_by = datetime.now(), user.label
        a.status = "endorse" if a.endorser_employee_id else "calibration"
    audit(db, user, "manager_review", "appraisal", a.id, f"{'sent' if submit else 'saved'}; overall {a.overall}")


def compute(a: Appraisal) -> None:
    w = (a.cycle.weights or DEFAULT_WEIGHTS)
    comp = [v.get("mgr") for v in (a.competencies or {}).values() if v.get("mgr")]
    a.comp_score = round(sum(comp) / len(comp), 2) if comp else None
    if a.form == "operator":
        ww = w.get("operator") or DEFAULT_WEIGHTS["operator"]
        parts = {k: (p or {}).get("score") for k, p in (a.parts or {}).items()}
        got = {k: v for k, v in parts.items() if v}
        tw = sum(ww.get(k, 0) for k in got)
        a.kpi_score = None
        a.overall = round(sum(v * ww.get(k, 0) for k, v in got.items()) / tw, 2) if tw else None
    else:
        items = [i for i in a.items or [] if i.get("mgr")]
        tw = sum(i.get("weight") or 0 for i in items)
        a.kpi_score = round(sum(i["mgr"] * (i.get("weight") or 0) for i in items) / tw, 2) if tw else None
        ww = w.get("manager" if a.employee.employee_group == "Manager" else "staff") or DEFAULT_WEIGHTS["staff"]
        if a.kpi_score is not None and a.comp_score is not None:
            a.overall = round((a.kpi_score * ww["kpi"] + a.comp_score * ww["comp"]) / (ww["kpi"] + ww["comp"]), 2)
        else:
            a.overall = a.kpi_score or a.comp_score
    a.proposed_rating = max(1, min(5, int(a.overall + 0.5))) if a.overall else None


def endorse(db: Session, user: User, a: Appraisal, note: str, rating: Optional[int]) -> None:
    _not_own(user, a)
    if a.status != "endorse" or not (user.employee_id == a.endorser_employee_id or user.has("hr_manager")):
        raise PerformError("Not waiting for your endorsement")
    if rating and rating != a.proposed_rating:
        if not (note or "").strip():
            raise PerformError("Write why the rating changes")
        a.final_rating = rating
    a.endorse_note, a.endorse_at, a.endorse_by = (note or "")[:2000] or None, datetime.now(), user.label
    a.status = "calibration"
    audit(db, user, "endorse", "appraisal", a.id, f"rating {letter(a.final_rating or a.proposed_rating)}")


def calibrate(db: Session, user: User, a: Appraisal, rating: int, note: str) -> None:
    _not_own(user, a)
    if a.cycle.status != "calibration" or a.status != "calibration":
        raise PerformError("Calibration is not open for this appraisal")
    if not user.has("hr_manager", "gm"):
        raise PerformError("The HR Manager or GM calibrates")
    if rating not in RATINGS:
        raise PerformError("Unknown rating")
    current = a.final_rating or a.proposed_rating
    if rating != current and not (note or "").strip():
        raise PerformError("Write why the rating changes")
    a.final_rating, a.calibrated_by = rating, user.label
    if note:
        a.calibration_note = note[:300]
    audit(db, user, "calibrate", "appraisal", a.id, f"{letter(current)} -> {letter(rating)}. {note}"[:300])


def release(db: Session, user: User, c: PerformanceCycle) -> int:
    """HR Manager closes the year: final ratings are fixed and people can see them."""
    from .growth import create_plan

    if not user.has("hr_manager"):
        raise PerformError("The HR Manager releases the results")
    if c.status != "calibration":
        raise PerformError("Move the cycle to calibration first")
    rows = list(db.scalars(select(Appraisal).where(Appraisal.cycle_id == c.id)))
    waiting = [a for a in rows if a.status not in ("calibration", "released")]
    if waiting:
        raise PerformError(f"{len(waiting)} appraisal(s) are not finished: " + ", ".join(a.employee_id for a in waiting[:8]))
    n = 0
    for a in rows:
        a.final_rating = a.final_rating or a.proposed_rating
        a.status = "released"
        if a.final_rating and a.final_rating <= 2 and not a.plan_id:
            plan = create_plan(db, user, a.employee, None)
            weakest = sorted(((v.get("mgr") or 5, k) for k, v in (a.competencies or {}).items()))[:2]
            from .growth import competencies, default_items

            plan.items = default_items([k for _, k in weakest], competencies(db))
            plan.title = f"Performance improvement plan {c.period_end.year}"
            plan.review_on = date.today() + timedelta(days=90)
            a.plan_id = plan.id
        n += 1
    c.status, c.released_at, c.released_by = "closed", datetime.now(), user.label
    audit(db, user, "release", "performance_cycle", c.id, f"{n} results released")
    return n


def acknowledge(db: Session, user: User, a: Appraisal, agree: bool, note: str) -> None:
    if user.employee_id != a.employee_id or a.status != "released":
        raise PerformError("Only the employee acknowledges a released result")
    a.ack_at, a.ack_agree, a.ack_note = datetime.now(), agree, (note or "")[:2000] or None
    audit(db, user, "acknowledge", "appraisal", a.id, "agrees" if agree else "does not agree")


# ---------- AI help ----------

KPI_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "kpi_code": {"type": ["string", "null"]}, "goal_id": {"type": ["integer", "null"]},
        "measure": {"type": "string"}, "unit": {"type": "string"}, "direction": {"type": "string", "enum": ["up", "down", "exact"]},
        "target": {"type": "number"}, "weight": {"type": "integer"}},
        "required": ["title", "kpi_code", "goal_id", "measure", "unit", "direction", "target", "weight"], "additionalProperties": False}},
        "review": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "ok": {"type": "boolean"}, "comment": {"type": "string"}},
                                              "required": ["title", "ok", "comment"], "additionalProperties": False}}},
    "required": ["items", "review"],
    "additionalProperties": False,
}

KPI_PROMPT = """You are a senior HR manager at a Thai block rubber (STR 20) factory helping a supervisor set
yearly KPIs for one person. {task}

Rules for good KPIs:
- 3 to 7 KPIs, weights add up to 100, each between 5 and 40.
- Each is measurable with a number target and a clear way to measure it, within the year.
- Link to a unit goal (goal_id) where the person really contributes; use a KPI library code when one fits.
- Things the person controls, not the whole company's result. No KPIs on attendance (the app scores it separately).
- Plain words a supervisor understands.

Person: {job}, grade {grade}, unit {unit}
Job purpose: {purpose}
Main duties: {duties}
Unit goals (id: title, target):
{goals}
KPI library (code: name, unit, direction):
{library}
Current draft:
{draft}"""


def ai_kpis(db: Session, a: Appraisal, mode: str) -> Optional[str]:
    """mode 'suggest': propose items when the draft is empty; 'check': review the current draft."""
    from .ai.providers import AIError, get_provider

    emp = a.employee
    goals = unit_goals(db, a.cycle, a.org_unit_code)
    lib = list(db.scalars(select(KpiDefinition).where(KpiDefinition.active.is_(True))))
    job = emp.job
    task = ("Propose the KPIs (items) and leave review empty." if mode == "suggest"
            else "Review each KPI in the current draft (review: ok and a one-line comment with a better wording or target), and return items empty.")
    prompt = KPI_PROMPT.format(
        task=task, job=job.title_en if job else "-", grade=emp.grade_code or "-", unit=emp.org_unit.name_en if emp.org_unit else "-",
        purpose=(job.purpose if job else None) or "-", duties=((job.responsibilities if job else None) or "-").replace("\n", "; "),
        goals="\n".join(f"- {g.id}: {g.title}, target {g.target:g}" for g in goals) or "-",
        library="\n".join(f"- {k.code}: {k.name_en}, {k.unit}, {k.direction}" for k in lib),
        draft="\n".join(f"- {i['title']}: target {i.get('target')}, weight {i.get('weight')}" for i in a.items or []) or "-",
    )
    try:
        res = get_provider().classify(prompt, KPI_SCHEMA, {"task": "kpi", "mode": mode, "goals": [(g.id, g.title, g.target, g.kpi_code) for g in goals],
                                                           "items": a.items or [], "job": job.title_en if job else ""})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    if mode == "suggest":
        codes = {k.code for k in lib}
        gids = {g.id for g in goals}
        items = []
        for it in (res.data.get("items") or [])[:7]:
            items.append({"id": uuid.uuid4().hex[:8], "title": str(it["title"])[:200], "kpi_code": it.get("kpi_code") if it.get("kpi_code") in codes else None,
                          "goal_id": it.get("goal_id") if it.get("goal_id") in gids else None, "measure": str(it.get("measure") or "")[:300],
                          "unit": str(it.get("unit") or "")[:20], "direction": it.get("direction") if it.get("direction") in DIRECTIONS else "up",
                          "target": it.get("target"), "weight": int(it.get("weight") or 0)})
        if not items:
            return "The AI answer had no usable KPIs"
        a.items = items
    else:
        a.facts = {**(a.facts or {}), "ai_review": [{"title": r["title"][:200], "ok": bool(r["ok"]), "comment": r["comment"][:400]} for r in res.data.get("review") or []]}
    a.ai_used = True
    return None


# ---------- numbers ----------

@dataclass
class CycleStats:
    total: int
    by_status: dict
    ratings: dict  # rating -> count (final or proposed)
    by_unit: dict  # unit -> {n, ratings}
    goals: list  # (goal, pct, score)


def stats(db: Session, c: PerformanceCycle, rows: list[Appraisal]) -> CycleStats:
    ratings = Counter(a.final_rating or a.proposed_rating for a in rows if (a.final_rating or a.proposed_rating))
    by_unit: dict[str, dict] = defaultdict(lambda: {"n": 0, "ratings": Counter(), "done": 0})
    for a in rows:
        u = by_unit[a.org_unit_code]
        u["n"] += 1
        r = a.final_rating or a.proposed_rating
        if r:
            u["ratings"][r] += 1
            u["done"] += 1
    goals = []
    for g in db.scalars(select(Goal).where(Goal.cycle_id == c.id).order_by(Goal.level, Goal.org_unit_code, Goal.id)):
        pct, s = goal_score(g)
        goals.append((g, pct, s))
    return CycleStats(total=len(rows), by_status=Counter(a.status for a in rows), ratings=dict(ratings), by_unit=dict(by_unit), goals=goals)


def guide_gaps(c: PerformanceCycle, ratings: dict) -> list[dict]:
    """Where the share of a rating is far from the guide (more than 10 points). A prompt to talk, never a quota."""
    n = sum(ratings.values())
    if n < 5:
        return []
    out = []
    for r, share in (c.guide or DEFAULT_GUIDE).items():
        got = round(100 * ratings.get(int(r), 0) / n)
        if abs(got - share) > 10:
            out.append(finding("flag", "cal_far", letter=letter(int(r)), got=got, guide=share))
    return out


def export_rows(db: Session, c: PerformanceCycle) -> list[list]:
    merit = c.merit_guide or DEFAULT_MERIT
    rows = [["employee_id", "name", "org_unit", "group", "rating", "letter", "overall", "merit_guideline_pct"]]
    for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id == c.id).order_by(Appraisal.employee_id)):
        r = a.final_rating
        rows.append([a.employee_id, a.employee.full_name_en, a.org_unit_code, a.employee.employee_group, r or "", letter(r),
                     a.overall if a.overall is not None else "", merit.get(str(r), "") if r else ""])
    return rows


def rule_en(f: dict) -> str:
    from ..i18n import rule_text

    return rule_text("en", f)
