"""Job grading (blueprint M7): a simple points method to place each job in a grade.

No pay here: grades carry a band label only, and salary ranges stay in Bplus and
HR practice. The factors and their levels are our own and a DRAFT for HR to agree."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Job

# factor -> (weight, name, [level 1..5 descriptions]); weights add up to 100, so points run 100-500
FACTORS = {
    "know": (25, "Knowledge and skill", [
        "Simple tasks learnt on the job in days",
        "Trained skills, follows set methods; a few weeks to learn",
        "Skilled trade or vocational level; solid know-how of one area",
        "Degree-level or deep specialist know-how of a field",
        "Expert across several fields or the whole business",
    ]),
    "solve": (20, "Problem solving", [
        "Follows clear instructions; problems go to the supervisor",
        "Picks from known answers to everyday problems",
        "Finds causes and fixes problems within own area",
        "Solves new or complex problems; changes methods",
        "Sets direction for problems with no precedent",
    ]),
    "impact": (25, "Responsibility and impact", [
        "Own tasks; mistakes are small and found quickly",
        "Own output and equipment; mistakes cost some time or material",
        "Results of a process or a small area; mistakes reach customers or safety",
        "Results of a section or a key function; large cost, quality or legal impact",
        "Results of a department or the company",
    ]),
    "people": (15, "Leading people", [
        "No one reports to the job",
        "Shows others how to do tasks; helps new staff",
        "Leads a small team or a shift day to day",
        "Manages supervisors or a section; hires and appraises",
        "Leads a department or several sections through managers",
    ]),
    "work": (15, "Working conditions and risk", [
        "Office, little physical effort",
        "Some standing, walking or lifting; normal factory areas",
        "Regular heavy work, heat, noise or shifts",
        "Hazardous machines or chemicals; strict safety rules",
        "Constant high risk or very hard conditions",
    ]),
}
STATUSES = {"draft": "Draft", "agreed": "Agreed"}


def points_of(levels: dict) -> Optional[int]:
    if any(levels.get(k) not in (1, 2, 3, 4, 5) for k in FACTORS):
        return None
    return sum(FACTORS[k][0] * levels[k] for k in FACTORS)


class JobEvaluation(Timestamps, Base):
    __tablename__ = "job_evaluation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_code: Mapped[str] = mapped_column(ForeignKey("job.code"), unique=True)
    levels: Mapped[dict] = mapped_column(JSON, default=dict)  # factor -> 1-5
    points: Mapped[Optional[int]] = mapped_column(Integer)
    suggested_grade: Mapped[Optional[str]] = mapped_column(String(20))
    agreed_grade: Mapped[Optional[str]] = mapped_column(String(20))
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), default="draft", index=True)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    evaluated_by: Mapped[Optional[str]] = mapped_column(String(160))
    agreed_by: Mapped[Optional[str]] = mapped_column(String(160))
    agreed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    job: Mapped[Job] = relationship()
