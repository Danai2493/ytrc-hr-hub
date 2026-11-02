"""Phase 5, plan ahead (blueprint M11, M12, L5): succession for critical positions and
records of stay talks. Leaving-risk flags and the workforce plan are computed, not stored.

Succession and risk flags are confidential: HR, the GM and the position's own management
line only. Risk flags start a supportive talk; they are never used for discipline or pay."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee, Position

READINESS = {"now": "Ready now", "1_2y": "Ready in 1-2 years", "3y": "Ready in 3+ years"}
VACANCY_RISK = {"low": "Low", "medium": "Medium", "high": "High"}


class SuccessionPlan(Timestamps, Base):
    __tablename__ = "succession_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("position.position_id"), unique=True)
    impact: Mapped[Optional[str]] = mapped_column(String(8))  # how hard it would hit if the seat were empty
    cover: Mapped[Optional[str]] = mapped_column(String(300))  # who covers in an emergency
    note: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(160))
    reviewed_on: Mapped[Optional[date]] = mapped_column(Date)

    position: Mapped[Position] = relationship()
    successors: Mapped[list["Successor"]] = relationship(back_populates="plan", cascade="all, delete-orphan", order_by="Successor.rank")


class Successor(Timestamps, Base):
    __tablename__ = "successor"
    __table_args__ = (UniqueConstraint("plan_id", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("succession_plan.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    readiness: Mapped[str] = mapped_column(String(6))
    rank: Mapped[int] = mapped_column(Integer, default=1)
    gaps: Mapped[Optional[str]] = mapped_column(String(300))  # what they still need: skills, experience
    added_by: Mapped[Optional[str]] = mapped_column(String(160))

    plan: Mapped[SuccessionPlan] = relationship(back_populates="successors")
    employee: Mapped[Employee] = relationship()


class StayTalk(Timestamps, Base):
    """A supportive talk after a leaving-risk flag: what the person said and what was agreed."""

    __tablename__ = "stay_talk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    on: Mapped[date] = mapped_column(Date)
    by: Mapped[Optional[str]] = mapped_column(String(160))
    note: Mapped[str] = mapped_column(Text)
    follow_up_on: Mapped[Optional[date]] = mapped_column(Date)
    created: Mapped[Optional[datetime]] = mapped_column(DateTime)
