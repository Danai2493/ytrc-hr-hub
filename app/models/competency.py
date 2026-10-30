"""Competency development (YTRC Growth Compass): framework, periodic
self-assessment with an optional supervisor view, and the individual
development plan. Results are for growth, not for rating people."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

ANSWER_SCALE = {1: "Rarely", 2: "Sometimes", 3: "Often", 4: "Usually", 5: "Almost always"}
AUDIENCES = {"core": "Everyone", "staff": "Staff and above", "leader": "Team leaders"}


class Competency(Timestamps, Base):
    __tablename__ = "competency"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # TH1
    cluster: Mapped[str] = mapped_column(String(10))  # THINK, PEOPLE, SELF, DELIVER, LEAD
    name_en: Mapped[str] = mapped_column(String(80))
    name_th: Mapped[Optional[str]] = mapped_column(String(120))
    definition_en: Mapped[Optional[str]] = mapped_column(Text)
    definition_th: Mapped[Optional[str]] = mapped_column(Text)
    leaders_only: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CompetencyQuestion(Timestamps, Base):
    __tablename__ = "competency_question"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competency_code: Mapped[str] = mapped_column(ForeignKey("competency.code"), index=True)
    audience: Mapped[str] = mapped_column(String(8), default="core")  # core / staff / leader
    text_en: Mapped[str] = mapped_column(String(300))
    text_th: Mapped[Optional[str]] = mapped_column(String(300))
    text_my: Mapped[Optional[str]] = mapped_column(String(400))
    reverse: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    competency: Mapped[Competency] = relationship()


class GrowthPath(Timestamps, Base):
    """A direction people can grow in. Each has the competency levels that
    help most; the report shows how close a person's profile is today."""

    __tablename__ = "growth_path"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(80))
    name_th: Mapped[Optional[str]] = mapped_column(String(120))
    description_en: Mapped[Optional[str]] = mapped_column(Text)
    description_th: Mapped[Optional[str]] = mapped_column(Text)
    targets: Mapped[dict] = mapped_column(JSON, default=dict)  # {competency: level}
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AssessmentRound(Timestamps, Base):
    __tablename__ = "assessment_round"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))  # "2027 round 1"
    opens_on: Mapped[date] = mapped_column(Date)
    closes_on: Mapped[date] = mapped_column(Date)
    groups: Mapped[list] = mapped_column(JSON, default=list)  # employee groups invited
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open / closed
    note: Mapped[Optional[str]] = mapped_column(String(300))
    created_by: Mapped[Optional[str]] = mapped_column(String(160))


class Assessment(Timestamps, Base):
    __tablename__ = "assessment"
    __table_args__ = (UniqueConstraint("round_id", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("assessment_round.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    audience: Mapped[str] = mapped_column(String(8), default="core")  # question set used
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open / submitted
    answers: Mapped[Optional[dict]] = mapped_column(JSON)  # {question id: 1-5}
    scores: Mapped[Optional[dict]] = mapped_column(JSON)  # {competency: 1.0-5.0}
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(8), default="web")  # web / paper
    entered_by: Mapped[Optional[str]] = mapped_column(String(160))
    manager_scores: Mapped[Optional[dict]] = mapped_column(JSON)  # {competency: 1-5}
    manager_note: Mapped[Optional[str]] = mapped_column(Text)
    manager_by: Mapped[Optional[str]] = mapped_column(String(160))
    manager_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    round: Mapped[AssessmentRound] = relationship()
    employee: Mapped[Employee] = relationship()


ACTION_TYPES = {"job": "Learn on the job (70%)", "people": "Learn from others: coaching, buddy (20%)", "course": "Course or reading (10%)"}


class DevelopmentPlan(Timestamps, Base):
    """Individual development plan: 1 to 3 focus areas, each with actions in
    the 70-20-10 mix, agreed by the employee and the supervisor."""

    __tablename__ = "development_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    assessment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("assessment.id"))
    title: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="draft", index=True)  # draft / agreed / done
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{competency, goal, actions:[{type, text, course, due, done}], progress}]
    career_interest: Mapped[Optional[str]] = mapped_column(String(10))  # growth path code
    employee_note: Mapped[Optional[str]] = mapped_column(Text)
    agreed_by: Mapped[Optional[str]] = mapped_column(String(160))
    agreed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_on: Mapped[Optional[date]] = mapped_column(Date)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)

    employee: Mapped[Employee] = relationship()
