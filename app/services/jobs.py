"""Job descriptions. Each Job holds one JD (purpose, responsibilities,
education, experience) and the skills it needs (JobSkill). AI drafts a first
version from the job title, unit and the skill list; HR edits and saves it."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, JobSkill, Position, Skill, User
from .common import audit

JD_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string"},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "education": {"type": "string"},
        "experience": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "properties": {"code": {"type": "string"}, "level": {"type": "integer"}},
                                              "required": ["code", "level"], "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["purpose", "responsibilities", "education", "experience", "skills", "notes"],
    "additionalProperties": False,
}

JD_PROMPT = """You are a senior HR manager at a Thai block rubber (STR 20) factory with about 240 staff,
many of them Myanmar workers. Write a short, plain job description for this job. Use simple English that
translates well into Thai and Burmese. 5 to 8 responsibilities, each one sentence starting with a verb.
Do not invent pay, benefits or legal promises.

Job: {title} ({family}), grade {grade}
Works in: {units}
Reports to: {reports}
Existing text (improve it, keep facts): {existing}

Choose the skills this job needs from this list ONLY, with the level a fully trained person holds
(1 = I in training, 2 = L with supervision, 3 = U alone to standard, 4 = O can teach others):
{skills}

In "notes", list anything HR must check (for example a legal licence or a medical check)."""


def units_for(db: Session, job: Job) -> list[str]:
    return sorted({p.org_unit.name_en for p in db.scalars(select(Position).where(Position.job_code == job.code, Position.active.is_(True)))})


def draft(db: Session, job: Job) -> tuple[Optional[dict], Optional[str]]:
    from .ai.providers import AIError, get_provider

    skills = list(db.scalars(select(Skill).where(Skill.active.is_(True)).order_by(Skill.sort_order)))
    units = units_for(db, job)
    current = [r.skill_code for r in db.scalars(select(JobSkill).where(JobSkill.job_code == job.code))]
    reports = {p.reports_to_position_id for p in db.scalars(select(Position).where(Position.job_code == job.code))} - {None}
    prompt = JD_PROMPT.format(
        title=job.title_en, family=job.job_family or "-", grade=job.default_grade_code or "-", units=", ".join(units) or "-",
        reports=", ".join(sorted(reports)) or "the Department Head",
        existing=(job.purpose or "") + " " + (job.responsibilities or "") or "none",
        skills="\n".join(f"- {s.code}: {s.name_en} ({s.category})" for s in skills),
    )
    hint = {"task": "jd", "title": job.title_en, "unit": units[0] if units else None,
            "skills": current or [s.code for s in skills if s.org_unit_code is None][:3]}
    try:
        res = get_provider().classify(prompt, JD_SCHEMA, hint)
    except AIError as e:
        return None, str(e)
    except Exception as e:  # network, bad JSON
        return None, f"{type(e).__name__}: {e}"[:300]
    data = res.data
    valid = {s.code for s in skills}
    data["skills"] = [x for x in data.get("skills") or [] if x.get("code") in valid and 1 <= int(x.get("level") or 0) <= 4]
    data["_provider"] = res.provider
    return data, None


def save(db: Session, user: User, job: Job, purpose: str, responsibilities: str, education: str, experience: str,
         skills: dict[str, int]) -> None:
    before = {"purpose": job.purpose, "responsibilities": job.responsibilities, "education": job.education, "experience": job.experience}
    job.purpose = (purpose or "").strip() or None
    job.responsibilities = (responsibilities or "").strip() or None
    job.education = (education or "").strip()[:200] or None
    job.experience = (experience or "").strip()[:200] or None
    have = {r.skill_code: r for r in db.scalars(select(JobSkill).where(JobSkill.job_code == job.code))}
    for code, lvl in skills.items():
        if lvl <= 0:
            if code in have:
                db.delete(have[code])
        elif code in have:
            have[code].level = lvl
        elif db.get(Skill, code):
            db.add(JobSkill(job_code=job.code, skill_code=code, level=lvl))
    db.flush()
    audit(db, user, "update", "job", job.code, "Job description and skills saved", {"before": before})
