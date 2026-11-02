"""Job grading by points. Rules of use are in app/models/grading.py."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, Grade, Job, JobEvaluation, Position, User
from ..models.grading import FACTORS, points_of
from .common import audit

HR = ("hr", "hr_manager")
# DRAFT points ladder for the current YTRC grades; HR Manager adjusts it on the grades page
DEFAULT_MIN_POINTS = {"Gardener": 100, "Operator": 115, "Technician": 135, "Technician 2": 155, "Leader": 175, "Staff 1": 195, "Staff 2": 215,
                      "S-Staff 1": 235, "S-Staff 2": 255, "SV": 280, "S-SV": 305, "S-Expert": 330, "Ass MG": 350, "MG": 375, "DGM": 410,
                      "GM": 440, "VP": 470, "MD": 490}

JOBEVAL_SCHEMA = {
    "type": "object",
    "properties": {"levels": {"type": "object", "properties": {k: {"type": "integer", "minimum": 1, "maximum": 5} for k in FACTORS},
                              "required": list(FACTORS), "additionalProperties": False},
                   "rationale": {"type": "string"}},
    "required": ["levels", "rationale"], "additionalProperties": False,
}
JOBEVAL_PROMPT = """You are a senior HR manager at a Thai block rubber (STR 20) factory doing job evaluation.
Rate the JOB (not the person in it) on each factor from 1 to 5, using these level descriptions:
{factors}

Job: {title} ({family}), unit grade today: {grade}
Purpose: {purpose}
Main duties: {duties}
Education: {education}. Experience: {experience}

Return the five levels and a short rationale (2-3 sentences) a Department Head can check."""


class GradingError(ValueError):
    pass


def ladder(db: Session) -> list[Grade]:
    return list(db.scalars(select(Grade).where(Grade.min_points.is_not(None)).order_by(Grade.min_points.desc())))


def suggest(db: Session, points: Optional[int]) -> Optional[str]:
    if points is None:
        return None
    for g in ladder(db):
        if points >= g.min_points:
            return g.code
    return None


def evaluation(db: Session, job: Job) -> JobEvaluation:
    ev = db.scalar(select(JobEvaluation).where(JobEvaluation.job_code == job.code))
    return ev or JobEvaluation(job_code=job.code, levels={}, status="draft")


def save(db: Session, user: User, job: Job, form) -> JobEvaluation:
    if not user.has(*HR):
        raise GradingError("Only HR evaluates jobs")
    ev = evaluation(db, job)
    lv = {}
    for k in FACTORS:
        try:
            lv[k] = int(form.get(f"f_{k}"))
        except (TypeError, ValueError):
            continue
    ev.levels = lv
    ev.points = points_of(lv)
    ev.suggested_grade = suggest(db, ev.points)
    ev.rationale = (form.get("rationale") or "").strip() or ev.rationale
    ev.evaluated_by = user.label
    if ev.status == "agreed":
        ev.status = "draft"  # changed after agreement: agree again
    db.add(ev)
    db.flush()
    audit(db, user, "job_eval", "job", job.code, f"{ev.points or '-'} points, suggests {ev.suggested_grade or '-'}")
    return ev


def agree(db: Session, user: User, job: Job, grade: str, reason: str) -> JobEvaluation:
    if not user.has("hr_manager"):
        raise GradingError("The HR Manager agrees job grades")
    ev = evaluation(db, job)
    if ev.points is None:
        raise GradingError("Rate all five factors first")
    if db.get(Grade, grade) is None:
        raise GradingError("Choose a grade")
    if grade != ev.suggested_grade:
        if not reason:
            raise GradingError("Say why the grade differs from the points")
        ev.rationale = ((ev.rationale or "") + f"\nGrade {grade} instead of {ev.suggested_grade}: {reason}").strip()
    ev.agreed_grade, ev.status, ev.agreed_by, ev.agreed_at = grade, "agreed", user.label, datetime.now()
    old = job.default_grade_code
    job.default_grade_code = grade
    audit(db, user, "job_grade", "job", job.code, f"grade {old or '-'} -> {grade}")
    return ev


def ai_suggest(db: Session, job: Job) -> Optional[str]:
    from .ai.providers import AIError, get_provider

    g = db.get(Grade, job.default_grade_code) if job.default_grade_code else None
    prompt = JOBEVAL_PROMPT.format(
        factors="\n".join(f"- {k} ({w}%) {n}: " + " | ".join(f"{i + 1}={d}" for i, d in enumerate(lv)) for k, (w, n, lv) in FACTORS.items()),
        title=job.title_en, family=job.job_family or "-", grade=job.default_grade_code or "-", purpose=job.purpose or "-",
        duties=(job.responsibilities or "-").replace("\n", "; "), education=job.education or "-", experience=job.experience or "-")
    try:
        res = get_provider().classify(prompt, JOBEVAL_SCHEMA, {"task": "jobeval", "title": job.title_en, "points": g.min_points if g else None})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    ev = evaluation(db, job)
    ev.levels = {k: max(1, min(5, int(v))) for k, v in (res.data.get("levels") or {}).items() if k in FACTORS}
    ev.points = points_of(ev.levels)
    ev.suggested_grade = suggest(db, ev.points)
    ev.rationale = str(res.data.get("rationale") or "")[:1000]
    ev.ai_used = True
    ev.status = "draft"
    db.add(ev)
    return None


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


def checks(db: Session) -> list[dict]:
    out = []
    jobs = [j for j in db.scalars(select(Job).where(Job.active.is_(True)).order_by(Job.code))]
    evs = {e.job_code: e for e in db.scalars(select(JobEvaluation))}
    ranks = {g.code: g.rank for g in db.scalars(select(Grade))}
    todo = [j.code for j in jobs if j.code not in evs or evs[j.code].status != "agreed"]
    if todo:
        out.append(finding("info", "gr_not_agreed", n=len(todo), total=len(jobs)))
    agreed = sorted([e for e in evs.values() if e.status == "agreed" and e.points], key=lambda e: e.points)
    for a in agreed:
        for b in agreed:
            if a.points < b.points and ranks.get(a.agreed_grade, 0) > ranks.get(b.agreed_grade, 0):
                out.append(finding("flag", "gr_inversion", a=a.job_code, pa=a.points, ga=a.agreed_grade, b=b.job_code, pb=b.points, gb=b.agreed_grade))
    jg = {j.code: j.default_grade_code for j in jobs}
    for p in db.scalars(select(Position).where(Position.job_code.is_not(None))):
        if p.grade_code and jg.get(p.job_code) and p.grade_code != jg[p.job_code] and p.job_code in evs and evs[p.job_code].status == "agreed":
            out.append(finding("flag", "gr_pos_differs", pos=p.position_id, g=p.grade_code, job=p.job_code, jg=jg[p.job_code]))
    groups: dict = {}
    for e in db.scalars(select(Employee).where(Employee.status == "Active", Employee.job_code.is_not(None))):
        if e.grade_code and jg.get(e.job_code) and e.grade_code != jg[e.job_code] and e.job_code in evs and evs[e.job_code].status == "agreed":
            groups.setdefault((e.job_code, e.grade_code), []).append(e.employee_id)
    for (job, g), ids in sorted(groups.items()):
        out.append(finding("info", "gr_emp_differs", n=len(ids), emp=", ".join(ids[:5]) + (" ..." if len(ids) > 5 else ""), g=g, job=job, jg=jg[job]))
    return out
