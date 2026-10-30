"""Competency development (YTRC Growth Compass).

HR opens an assessment round; each invited employee answers behaviour
questions about how often they do things. Scores per competency are the
average answer (1-5). The report shows strengths, areas to develop, the
change since the last round, the supervisor's view where given, and how close
the profile is to each growth path. The employee and supervisor then agree an
individual development plan (70-20-10), with AI suggesting actions.

No overall score, no ranking, no good/bad labels: the report compares the
person with their own profile and with paths they may choose."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Assessment,
    AssessmentRound,
    Competency,
    CompetencyQuestion,
    DevelopmentPlan,
    Employee,
    GrowthPath,
    OrgUnit,
    TrainingNeed,
    User,
)
from ..models.competency import ACTION_TYPES
from .common import audit

LEADER_JOBS = ("PRD-SV", "PRD-LDR", "MGR-DEPT", "GM")
LEADER_GRADES = ("MD", "VP", "GM", "DGM", "MG", "Ass MG", "S-SV", "SV", "Leader")
MIN_GROUP = 5  # team averages are shown only for 5 or more people, so no one can be singled out


class GrowthError(Exception):
    pass


def audience_for(db: Session, emp: Employee) -> str:
    heads = db.scalar(select(OrgUnit.code).where(OrgUnit.head_employee_id == emp.employee_id))
    if emp.job_code in LEADER_JOBS or emp.grade_code in LEADER_GRADES or heads:
        return "leader"
    return "core" if emp.employee_group == "Operator" else "staff"


def questions_for(db: Session, audience: str) -> list[CompetencyQuestion]:
    allowed = {"core": ("core",), "staff": ("core", "staff"), "leader": ("core", "staff", "leader")}[audience]
    return list(db.scalars(select(CompetencyQuestion).where(CompetencyQuestion.active.is_(True), CompetencyQuestion.audience.in_(allowed))
                          .order_by(CompetencyQuestion.sort_order)))


def competencies(db: Session) -> dict[str, Competency]:
    return {c.code: c for c in db.scalars(select(Competency).where(Competency.active.is_(True)).order_by(Competency.sort_order))}


# ---------- rounds ----------

def open_round(db: Session, user: User, name: str, opens: date, closes: date, groups: list[str], note: str = "") -> AssessmentRound:
    if closes < opens:
        raise GrowthError("The round must close after it opens")
    if not groups:
        raise GrowthError("Choose at least one employee group")
    rnd = AssessmentRound(name=name[:80], opens_on=opens, closes_on=closes, groups=groups, note=note[:300] or None, created_by=user.label)
    db.add(rnd)
    db.flush()
    n = 0
    for e in db.scalars(select(Employee).where(Employee.status == "Active", Employee.employee_group.in_(groups))):
        db.add(Assessment(round_id=rnd.id, employee_id=e.employee_id, audience=audience_for(db, e)))
        n += 1
    db.flush()
    audit(db, user, "create", "assessment_round", rnd.id, f"{rnd.name}: {n} people invited")
    return rnd


def close_round(db: Session, user: User, rnd: AssessmentRound) -> None:
    rnd.status = "closed"
    audit(db, user, "close", "assessment_round", rnd.id, rnd.name)


def score(db: Session, answers: dict) -> dict[str, float]:
    qs = {q.id: q for q in db.scalars(select(CompetencyQuestion))}
    by: dict[str, list[int]] = defaultdict(list)
    for qid, v in answers.items():
        q = qs.get(int(qid))
        if q is None:
            continue
        v = int(v)
        by[q.competency_code].append(6 - v if q.reverse else v)
    return {c: round(sum(v) / len(v), 2) for c, v in by.items() if v}


def save_answers(db: Session, a: Assessment, user: User, raw: dict, submit: bool, source: str = "web") -> None:
    if a.status == "submitted":
        raise GrowthError("Already sent")
    if a.round.status != "open":
        raise GrowthError("This round is closed")
    qs = questions_for(db, a.audience)
    answers = dict(a.answers or {})
    for q in qs:
        v = raw.get(str(q.id))
        if v in (None, ""):
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 5:
            answers[str(q.id)] = v
    a.answers = answers
    if submit:
        missing = [q for q in qs if str(q.id) not in answers]
        if missing:
            raise GrowthError(f"{len(missing)} question(s) not answered yet")
        a.scores = score(db, answers)
        a.status = "submitted"
        a.submitted_at = datetime.now()
        a.source = source
        a.entered_by = user.label
        audit(db, user, "submit", "assessment", a.id, f"{a.employee_id} {a.round.name} ({source})")


def manager_view(db: Session, a: Assessment, user: User, scores: dict, note: str) -> None:
    comps = competencies(db)
    clean = {}
    for code, v in scores.items():
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if code in comps and 1 <= v <= 5:
            clean[code] = v
    a.manager_scores = clean or None
    a.manager_note = (note or "")[:2000] or None
    a.manager_by, a.manager_at = user.label, datetime.now()
    audit(db, user, "manager_view", "assessment", a.id, f"{a.employee_id}: {len(clean)} competencies")


# ---------- report ----------

@dataclass
class PathFit:
    path: GrowthPath
    fit: int
    gaps: list[tuple[str, float, float]] = field(default_factory=list)  # (competency, score, target)


def path_fit(scores: dict[str, float], path: GrowthPath) -> Optional[PathFit]:
    t = {c: v for c, v in (path.targets or {}).items() if c in scores}
    if len(t) < 3:
        return None  # not enough of the path is measured for this person
    short = [max(0.0, v - scores[c]) for c, v in t.items()]
    fit = round(100 * (1 - sum(short) / len(short) / 4))
    gaps = sorted([(c, scores[c], v) for c, v in t.items() if scores[c] < v], key=lambda x: x[1] - x[2])
    return PathFit(path, fit, gaps)


@dataclass
class Report:
    a: Assessment
    comps: dict[str, Competency]
    scores: dict[str, float]
    clusters: dict[str, float]
    strengths: list[str]
    develop: list[str]
    previous: Optional[Assessment]
    delta: dict[str, float]
    paths: list[PathFit]
    views_differ: list[tuple[str, float, int]]


def report(db: Session, a: Assessment) -> Report:
    comps = competencies(db)
    s = a.scores or {}
    by_cl: dict[str, list[float]] = defaultdict(list)
    for c, v in s.items():
        if c in comps:
            by_cl[comps[c].cluster].append(v)
    clusters = {k: round(sum(v) / len(v), 2) for k, v in by_cl.items()}
    order = sorted(s, key=lambda c: (-s[c], comps[c].sort_order if c in comps else 0))
    strengths = [c for c in order if s[c] >= 3.5][:4] or order[:2]
    develop = [c for c in reversed(order) if c not in strengths][:3]
    prev = db.scalars(select(Assessment).where(Assessment.employee_id == a.employee_id, Assessment.status == "submitted",
                                               Assessment.submitted_at < (a.submitted_at or datetime.now()), Assessment.id != a.id)
                      .order_by(Assessment.submitted_at.desc())).first()
    delta = {c: round(v - prev.scores[c], 2) for c, v in s.items() if prev and prev.scores and c in prev.scores}
    paths = [f for f in (path_fit(s, p) for p in db.scalars(select(GrowthPath).where(GrowthPath.active.is_(True)))) if f]
    paths.sort(key=lambda f: -f.fit)
    differ = [(c, s[c], m) for c, m in (a.manager_scores or {}).items() if c in s and abs(s[c] - m) >= 1]
    return Report(a, comps, s, clusters, strengths, develop, prev, delta, paths, differ)


def can_see(db: Session, user: User, emp: Employee) -> bool:
    from .common import user_scope

    if user.employee_id == emp.employee_id:
        return True
    scope = user_scope(db, user)
    return scope.all or emp.org_unit_code in scope.org_units


def is_supervisor_of(db: Session, user: User, emp: Employee) -> bool:
    from .common import user_scope

    if user.employee_id == emp.employee_id or not user.has("supervisor", "dept_head", "hr_manager"):
        return False
    scope = user_scope(db, user)
    return scope.all or emp.org_unit_code in scope.org_units


# ---------- development plan ----------

# A small built-in library so the plan works without AI. code: [(type, text)]
ACTION_LIBRARY = {
    "TH1": [("job", "Lead a why-why analysis on the next repeat problem in your area"), ("people", "Join QA or Maintenance for one root-cause meeting"), ("course", "Problem solving basics (why-why, fishbone)")],
    "TH2": [("job", "Plan the shift preparation list for one week and check it daily"), ("people", "Ask your supervisor to review your weekly plan for a month"), ("course", "Time and task planning")],
    "TH3": [("job", "Learn one new machine or task in your area this quarter"), ("people", "Pair with an O-level colleague for two shifts"), ("course", "Read the work instruction and explain it back to your trainer")],
    "TH4": [("job", "Submit one improvement idea a month and test the best one"), ("people", "Join the next kaizen or 5S event"), ("course", "Kaizen and waste basics")],
    "PE1": [("job", "Give the shift handover using a short checklist for a month"), ("people", "Ask a colleague for feedback on how clear your handover is"), ("course", "Clear speaking and short report writing")],
    "PE2": [("job", "Offer help to a colleague who is behind, once a shift"), ("people", "Agree one shared team goal with your colleagues"), ("course", "Working in a mixed Thai and Myanmar team")],
    "PE3": [("job", "Take one task that needs another department and see it through"), ("people", "Spend half a day with the next department to see their work"), ("course", "Negotiation and influence basics")],
    "PE4": [("job", "Ask the next process each week what problems they get from you"), ("people", "Visit QA or Shipping to see customer complaints"), ("course", "Customer requirements for STR rubber")],
    "SE1": [("job", "Keep a simple log of promises made and kept for one month"), ("people", "Agree with your supervisor one rule to keep 100% this quarter"), ("course", "Company rules and why they exist")],
    "SE2": [("job", "After a hard shift, write down one thing that went well and one to change"), ("people", "Talk with a senior colleague about how they handle rush orders"), ("course", "Managing stress at work")],
    "SE3": [("job", "Double-check your records against the actual weights or counts for a week"), ("people", "Discuss a real integrity case with HR"), ("course", "Company code of conduct")],
    "SE4": [("job", "Set one learning goal with a date and track it"), ("people", "Ask two people for feedback every month"), ("course", "Choose one course from the training list")],
    "DE1": [("job", "Do a self-check of your output against the standard every hour"), ("people", "Spend a shift with a QA inspector"), ("course", "Quality standard for your product and area")],
    "DE2": [("job", "Lead the safety talk at the start of the shift once a week"), ("people", "Join the safety officer on a walk-through"), ("course", "Safety refresher, lock-out tag-out")],
    "DE3": [("job", "Track your daily target on a board and review it at the end of the shift"), ("people", "Ask a high performer how they plan their day"), ("course", "Goal setting and follow-up")],
    "DE4": [("job", "Measure and cut one waste in your area (rubber, water, power)"), ("people", "Work with Maintenance on one cost-saving idea"), ("course", "Cost awareness for operators")],
    "LE1": [("job", "Train one new person to U level and sign it off in the skills matrix"), ("people", "Be coached by an experienced trainer while you teach"), ("course", "On-the-job instruction (job instruction method)")],
    "LE2": [("job", "Hold a five-minute team meeting at the start of every shift"), ("people", "Ask your manager to observe one meeting and give feedback"), ("course", "Supervisor basics: OT rules, leave, safety talk")],
    "LE3": [("job", "Write down the facts and reasons for your next three decisions"), ("people", "Review a past decision with your manager"), ("course", "Decision making for supervisors")],
    "LE4": [("job", "Lead one change in your area from explanation to follow-up"), ("people", "Find a mentor who has led a change well"), ("course", "Leading change")],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "competency": {"type": "string"}, "goal": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object", "properties": {"type": {"type": "string", "enum": list(ACTION_TYPES)}, "text": {"type": "string"}},
                                                    "required": ["type", "text"], "additionalProperties": False}}},
            "required": ["competency", "goal", "actions"], "additionalProperties": False}},
        "note": {"type": "string"},
    },
    "required": ["items", "note"],
    "additionalProperties": False,
}

PLAN_PROMPT = """You are an experienced HR development coach at a Thai block rubber factory.
Suggest a practical individual development plan for one employee. Use plain, kind language that a
factory worker understands; no judgement words like weak or poor. For each focus competency write one
goal that can be seen in daily work within 3 to 6 months, and 3 actions in the 70-20-10 mix:
"job" (learn by doing in the current job), "people" (coaching, buddy, shadowing), "course" (training or
reading). Prefer actions that cost little and fit shift work.

Job: {job} in {unit}. Employee group: {group}.
Focus competencies (code: name, definition, self score 1-5):
{focus}
Strengths to build on: {strengths}
Skill gaps in the skills matrix: {skill_gaps}
Career interest: {interest}
Courses the company runs: {courses}

Use the competency codes given. In "note", give one sentence for the supervisor on how to support."""


def default_items(focus: list[str], comps: dict[str, Competency]) -> list[dict]:
    items = []
    for c in focus:
        lib = ACTION_LIBRARY.get(c, [])
        items.append({"competency": c, "goal": f"Show {comps[c].name_en.lower()} in daily work, seen by my supervisor" if c in comps else "",
                      "actions": [{"type": t, "text": x, "done": False} for t, x in lib], "progress": 0})
    return items


def create_plan(db: Session, user: User, emp: Employee, a: Optional[Assessment]) -> DevelopmentPlan:
    comps = competencies(db)
    focus = report(db, a).develop[:2] if a and a.scores else []
    plan = DevelopmentPlan(employee_id=emp.employee_id, assessment_id=a.id if a else None,
                           title=f"Development plan {date.today().year}", items=default_items(focus, comps),
                           review_on=date.today() + timedelta(days=90))
    plan.employee = emp
    db.add(plan)
    db.flush()
    audit(db, user, "create", "development_plan", plan.id, f"IDP for {emp.employee_id}")
    return plan


def suggest(db: Session, plan: DevelopmentPlan, focus: list[str]) -> Optional[str]:
    """AI rewrites the actions for the chosen focus areas. Returns an error message or None."""
    from ..models import Course
    from .ai.providers import AIError, get_provider
    from .skills import matrix

    comps = competencies(db)
    focus = [c for c in focus if c in comps][:3]
    if not focus:
        return "Choose one to three competencies to focus on"
    emp = plan.employee
    a = db.get(Assessment, plan.assessment_id) if plan.assessment_id else None
    s = (a.scores or {}) if a else {}
    rep = report(db, a) if a and a.scores else None
    m = matrix(db, [emp])
    gaps = [sk.name_en for sk in m.skills if m.cells[(emp.employee_id, sk.code)]["gap"]]
    interest = db.get(GrowthPath, plan.career_interest) if plan.career_interest else None
    prompt = PLAN_PROMPT.format(
        job=emp.job.title_en if emp.job else "-", unit=emp.org_unit.name_en if emp.org_unit else "-", group=emp.employee_group,
        focus="\n".join(f"- {c}: {comps[c].name_en}. {comps[c].definition_en} Score {s.get(c, '-')}" for c in focus),
        strengths=", ".join(comps[c].name_en for c in (rep.strengths if rep else [])) or "-",
        skill_gaps=", ".join(gaps) or "none", interest=interest.name_en if interest else "not chosen",
        courses=", ".join(c.name_en for c in db.scalars(select(Course).where(Course.active.is_(True)))),
    )
    try:
        res = get_provider().classify(prompt, PLAN_SCHEMA, {"task": "idp", "focus": focus})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    items = []
    for it in res.data.get("items") or []:
        if it.get("competency") not in comps:
            continue
        acts = [{"type": x["type"], "text": str(x["text"])[:300], "done": False} for x in it.get("actions") or [] if x.get("type") in ACTION_TYPES][:5]
        items.append({"competency": it["competency"], "goal": str(it.get("goal") or "")[:300], "actions": acts, "progress": 0})
    if not items:
        return "The AI answer had no usable actions"
    plan.items = items
    plan.employee_note = plan.employee_note or None
    plan.ai_used = True
    return None


def save_plan(db: Session, user: User, plan: DevelopmentPlan, form) -> None:
    comps = competencies(db)
    items = []
    for i in range(3):
        c = form.get(f"c{i}")
        if not c or c not in comps:
            continue
        acts = []
        for j in range(5):
            text = (form.get(f"a{i}_{j}") or "").strip()
            if text:
                t = form.get(f"t{i}_{j}") if form.get(f"t{i}_{j}") in ACTION_TYPES else "job"
                acts.append({"type": t, "text": text[:300], "course": form.get(f"k{i}_{j}") or None, "due": form.get(f"d{i}_{j}") or None,
                             "done": form.get(f"x{i}_{j}") == "on"})
        try:
            prog = max(0, min(100, int(form.get(f"p{i}") or 0)))
        except ValueError:
            prog = 0
        items.append({"competency": c, "goal": (form.get(f"g{i}") or "")[:300], "actions": acts, "progress": prog})
    plan.items = items
    plan.career_interest = form.get("career_interest") or None
    plan.employee_note = (form.get("employee_note") or "")[:2000] or None
    try:
        plan.review_on = date.fromisoformat(form.get("review_on")) if form.get("review_on") else plan.review_on
    except ValueError:
        pass
    audit(db, user, "update", "development_plan", plan.id, f"{len(items)} focus area(s)")


def agree(db: Session, user: User, plan: DevelopmentPlan) -> int:
    """Supervisor agrees the plan. Course actions become training needs so HR can plan classes."""
    if not plan.items:
        raise GrowthError("Add at least one focus area first")
    plan.status = "agreed"
    plan.agreed_by, plan.agreed_at = user.label, datetime.now()
    n = 0
    for it in plan.items:
        for act in it.get("actions") or []:
            if act.get("type") == "course" and act.get("course"):
                exists = db.scalar(select(TrainingNeed).where(TrainingNeed.employee_id == plan.employee_id, TrainingNeed.course_code == act["course"],
                                                              TrainingNeed.status.in_(("open", "planned"))))
                if exists is None:
                    due = None
                    try:
                        due = date.fromisoformat(act["due"]) if act.get("due") else None
                    except ValueError:
                        pass
                    db.add(TrainingNeed(employee_id=plan.employee_id, course_code=act["course"], reason="idp", priority=2,
                                        due_date=due or plan.review_on, note=f"IDP: {it.get('goal') or it.get('competency')}"[:300]))
                    n += 1
    audit(db, user, "agree", "development_plan", plan.id, f"Agreed; {n} course(s) sent to training needs")
    return n


# ---------- team view ----------

def round_summary(db: Session, rnd: AssessmentRound, emp_ids: Optional[set[str]] = None) -> dict:
    rows = [a for a in db.scalars(select(Assessment).where(Assessment.round_id == rnd.id)) if emp_ids is None or a.employee_id in emp_ids]
    done = [a for a in rows if a.status == "submitted"]
    comps = competencies(db)
    by_unit: dict[str, list[Assessment]] = defaultdict(list)
    for a in done:
        by_unit[a.employee.org_unit_code].append(a)
    units = {}
    for u, items in by_unit.items():
        if len(items) < MIN_GROUP:
            units[u] = {"n": len(items), "hidden": True}
            continue
        cl: dict[str, list[float]] = defaultdict(list)
        for a in items:
            for c, v in (a.scores or {}).items():
                if c in comps:
                    cl[comps[c].cluster].append(v)
        units[u] = {"n": len(items), "hidden": False, "clusters": {k: round(sum(v) / len(v), 2) for k, v in cl.items()}}
    comp_avg: dict[str, list[float]] = defaultdict(list)
    for a in done:
        for c, v in (a.scores or {}).items():
            comp_avg[c].append(v)
    company = {c: round(sum(v) / len(v), 2) for c, v in comp_avg.items() if len(v) >= MIN_GROUP}
    plans = db.scalars(select(DevelopmentPlan).where(DevelopmentPlan.assessment_id.in_([a.id for a in done]))).all() if done else []
    return {"invited": len(rows), "done": len(done), "units": units, "company": company,
            "plans": len(plans), "agreed": sum(1 for p in plans if p.status in ("agreed", "done"))}
