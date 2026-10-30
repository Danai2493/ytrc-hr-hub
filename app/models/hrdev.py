"""HR Development tables (Phase 2): leave and exit (blueprint M4, M9)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

LEAVE_STATUSES = ["pending", "approved", "rejected", "cancelled"]
LEAVE_PARTS = {"full": "Full day", "am": "Morning (half day)", "pm": "Afternoon (half day)"}


class LeaveType(Timestamps, Base):
    """One kind of leave. The defaults follow the Labour Protection Act; HR can
    change days and rules in Admin when company policy gives more."""

    __tablename__ = "leave_type"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)  # SICK, PERS, VAC ...
    name_en: Mapped[str] = mapped_column(String(80))
    name_th: Mapped[Optional[str]] = mapped_column(String(80))
    day_code: Mapped[str] = mapped_column(String(2))  # time sheet code: s, p, v, m, u, c
    paid: Mapped[bool] = mapped_column(Boolean, default=True)
    days_per_year: Mapped[Optional[float]] = mapped_column(Float)  # None = no yearly limit
    count_calendar_days: Mapped[bool] = mapped_column(Boolean, default=False)  # maternity counts every day
    document_from_days: Mapped[Optional[int]] = mapped_column(Integer)  # e.g. medical certificate from 3 days
    min_service_days: Mapped[int] = mapped_column(Integer, default=0)  # vacation after 1 year
    notice_days: Mapped[int] = mapped_column(Integer, default=0)  # ask this many days ahead
    counts_as_absence: Mapped[bool] = mapped_column(Boolean, default=True)  # for the absence rate
    gender: Mapped[Optional[str]] = mapped_column(String(1))  # F for maternity
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class LeaveRequest(Timestamps, Base):
    __tablename__ = "leave_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # LV-2026-000123
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    leave_type: Mapped[str] = mapped_column(ForeignKey("leave_type.code"))
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    part: Mapped[str] = mapped_column(String(4), default="full")  # full / am / pm (one-day leave only)
    days: Mapped[float] = mapped_column(Float, default=0)  # working days taken
    reason: Mapped[Optional[str]] = mapped_column(String(300))
    has_document: Mapped[bool] = mapped_column(Boolean, default=False)  # e.g. medical certificate seen
    capture_id: Mapped[Optional[int]] = mapped_column(Integer)  # photo of the paper form or certificate
    source: Mapped[str] = mapped_column(String(10), default="web")  # web / paper
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    flags: Mapped[Optional[list]] = mapped_column(JSON)
    submitted_by: Mapped[Optional[str]] = mapped_column(String(160))
    approval_id: Mapped[Optional[int]] = mapped_column(ForeignKey("approval_request.id"))

    employee: Mapped[Employee] = relationship()
    type: Mapped[LeaveType] = relationship()

    def covers(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


EXIT_INTERVIEW_QUESTIONS = [
    ("job", "The work itself"),
    ("boss", "My supervisor"),
    ("team", "Team and colleagues"),
    ("pay", "Pay and benefits"),
    ("shift", "Shift and workload"),
    ("growth", "Training and chances to grow"),
    ("safety", "Safety and working conditions"),
]


class ExitCase(Timestamps, Base):
    """Everything after a resignation is approved: clearance checklist, final
    pay notes, social security notice and the exit interview."""

    __tablename__ = "exit_case"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # EX-2026-000012
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    last_day: Mapped[date] = mapped_column(Date, index=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(16))  # from the exit event
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open / closed
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{code, label, owner, done, by, at, note}]
    vacation_days_left: Mapped[Optional[float]] = mapped_column(Float)  # paid out in final pay (LPA s.67)
    interview: Mapped[Optional[dict]] = mapped_column(JSON)  # {scores, would_return, main_reason, comments, by, at}
    ai_reason_code: Mapped[Optional[str]] = mapped_column(String(16))
    ai_confidence: Mapped[Optional[float]] = mapped_column(Float)
    ai_summary: Mapped[Optional[str]] = mapped_column(Text)
    ai_themes: Mapped[Optional[list]] = mapped_column(JSON)
    reason_confirmed_by: Mapped[Optional[str]] = mapped_column(String(160))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approval_id: Mapped[Optional[int]] = mapped_column(Integer)  # the approved exit event

    employee: Mapped[Employee] = relationship()

    @property
    def open_items(self) -> list[dict]:
        return [i for i in (self.items or []) if not i.get("done")]
