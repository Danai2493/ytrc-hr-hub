"""HR Development tables (Phase 2, part 2): skills matrix (ILUO), training,
onboarding and probation (blueprint M4, M8, M9 in the blueprint numbering)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

# ILUO: the level shown as a quarter-filled circle on the shop-floor board.
LEVELS = {
    0: ("-", "Not trained"),
    1: ("I", "In training: works only with a trainer"),
    2: ("L", "Can do the job with supervision"),
    3: ("U", "Can do the job alone, to standard"),
    4: ("O", "Can teach others and improve the job"),
}
SKILL_CATEGORIES = ["Process", "Machine", "Quality", "Safety", "Legal licence", "Office", "Leadership"]


class Skill(Timestamps, Base):
    __tablename__ = "skill"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)  # SK-DRY-01
    name_en: Mapped[str] = mapped_column(String(120))
    name_th: Mapped[Optional[str]] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(20), default="Process")
    org_unit_code: Mapped[Optional[str]] = mapped_column(ForeignKey("org_unit.code"))  # main unit; None = everyone
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    min_uo: Mapped[int] = mapped_column(Integer, default=0)  # people needed at U or O to run all shifts safely
    course_code: Mapped[Optional[str]] = mapped_column(String(20))  # course that closes a gap
    valid_months: Mapped[Optional[int]] = mapped_column(Integer)  # licence or refresher, e.g. forklift
    description: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class JobSkill(Base):
    """What the job needs: the level a fully trained person in this job holds."""

    __tablename__ = "job_skill"

    job_code: Mapped[str] = mapped_column(ForeignKey("job.code"), primary_key=True)
    skill_code: Mapped[str] = mapped_column(ForeignKey("skill.code"), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, default=3)

    skill: Mapped[Skill] = relationship()


class EmployeeSkill(Timestamps, Base):
    __tablename__ = "employee_skill"
    __table_args__ = (UniqueConstraint("employee_id", "skill_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    skill_code: Mapped[str] = mapped_column(ForeignKey("skill.code"), index=True)
    level: Mapped[int] = mapped_column(Integer, default=0)
    assessed_on: Mapped[Optional[date]] = mapped_column(Date)
    assessed_by: Mapped[Optional[str]] = mapped_column(String(160))
    valid_until: Mapped[Optional[date]] = mapped_column(Date)  # licences and refreshers
    note: Mapped[Optional[str]] = mapped_column(String(300))
    history: Mapped[Optional[list]] = mapped_column(JSON)  # [{level, on, by}]

    employee: Mapped[Employee] = relationship()
    skill: Mapped[Skill] = relationship()


class Course(Timestamps, Base):
    __tablename__ = "course"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)  # CR-SAF-01
    name_en: Mapped[str] = mapped_column(String(160))
    name_th: Mapped[Optional[str]] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(12), default="internal")  # internal / external / OJT
    hours: Mapped[float] = mapped_column(Float, default=1)
    legal: Mapped[bool] = mapped_column(Boolean, default=False)  # required by law (safety, fire, forklift)
    valid_months: Mapped[Optional[int]] = mapped_column(Integer)
    skill_code: Mapped[Optional[str]] = mapped_column(ForeignKey("skill.code"))
    skill_level: Mapped[Optional[int]] = mapped_column(Integer)  # level reached when passed (OJT sign-off)
    for_new_staff: Mapped[bool] = mapped_column(Boolean, default=False)  # part of induction
    active: Mapped[bool] = mapped_column(Boolean, default=True)


TRAINING_RESULTS = ["attended", "passed", "failed"]


class TrainingRecord(Timestamps, Base):
    __tablename__ = "training_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    course_code: Mapped[str] = mapped_column(ForeignKey("course.code"), index=True)
    session_ref: Mapped[Optional[str]] = mapped_column(String(20), index=True)  # TR-2026-000004: one class
    on_date: Mapped[date] = mapped_column(Date, index=True)
    hours: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[str] = mapped_column(String(10), default="attended")
    trainer: Mapped[Optional[str]] = mapped_column(String(120))
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    note: Mapped[Optional[str]] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(10), default="web")  # web / paper / import
    capture_id: Mapped[Optional[int]] = mapped_column(Integer)
    recorded_by: Mapped[Optional[str]] = mapped_column(String(160))

    employee: Mapped[Employee] = relationship()
    course: Mapped[Course] = relationship()


NEED_REASONS = {"gap": "Skill below the job's level", "expiry": "Licence or refresher due", "legal": "Required by law",
                "new_staff": "Induction for new staff", "coverage": "Too few people at U/O", "manual": "Asked by supervisor"}


class TrainingNeed(Timestamps, Base):
    __tablename__ = "training_need"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    skill_code: Mapped[Optional[str]] = mapped_column(ForeignKey("skill.code"))
    course_code: Mapped[Optional[str]] = mapped_column(ForeignKey("course.code"))
    reason: Mapped[str] = mapped_column(String(12), default="gap")
    current_level: Mapped[Optional[int]] = mapped_column(Integer)
    target_level: Mapped[Optional[int]] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1 high, 2 normal, 3 low
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open / planned / done / dropped
    note: Mapped[Optional[str]] = mapped_column(String(300))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    employee: Mapped[Employee] = relationship()
    skill: Mapped[Optional[Skill]] = relationship()
    course: Mapped[Optional[Course]] = relationship()


class OnboardingCase(Timestamps, Base):
    __tablename__ = "onboarding_case"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # ON-2026-000003
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    buddy_employee_id: Mapped[Optional[str]] = mapped_column(String(12))
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{code, label, owner, due, done, by, at, note}]
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    employee: Mapped[Employee] = relationship()

    @property
    def open_items(self) -> list[dict]:
        return [i for i in (self.items or []) if not i.get("done")]


PROBATION_CRITERIA = [
    ("quality", "Quality of work"),
    ("speed", "Speed and output"),
    ("safety", "Follows safety rules"),
    ("discipline", "Attendance and discipline"),
    ("teamwork", "Teamwork and attitude"),
    ("learning", "Learns the job"),
]
PROBATION_OUTCOMES = {"pass": "Pass probation", "fail": "Does not pass: end employment before the probation ends"}


class ProbationReview(Timestamps, Base):
    __tablename__ = "probation_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # PR-2026-000007
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    probation_end: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open / submitted / passed / failed / cancelled
    scores: Mapped[Optional[dict]] = mapped_column(JSON)
    facts: Mapped[Optional[dict]] = mapped_column(JSON)  # absences, late days, skills at review time
    recommendation: Mapped[Optional[str]] = mapped_column(String(6))
    comment: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(160))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approval_id: Mapped[Optional[int]] = mapped_column(ForeignKey("approval_request.id"))

    employee: Mapped[Employee] = relationship()
