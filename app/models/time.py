"""Time, OT and pay input tables (M2, M3)."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

# Day codes used on today's time sheets.
DAY_CODES = {
    "1": "Worked",
    "h": "Holiday / rest day",
    "a": "Absent",
    "v": "Vacation",
    "s": "Sick leave",
    "p": "Personal leave",
    "c": "Company leave",
    "m": "Maternity leave",
    "u": "Unpaid leave",
    "l": "Other leave",
}

# Pay codes handed to Bplus. Rate factors are applied in Bplus, the app sends hours.
PAY_CODES = {
    "OT15": "Workday OT x1.5",
    "HOL10": "Holiday work x1 (monthly pay)",
    "HOL20": "Holiday work x2 (daily pay)",
    "OT30": "Holiday OT x3",
}


class Shift(Timestamps, Base):
    __tablename__ = "shift"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)
    break_minutes: Mapped[int] = mapped_column(Integer, default=60)

    @property
    def is_night(self) -> bool:
        return self.end <= self.start


class Calendar(Timestamps, Base):
    __tablename__ = "calendar"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)  # OFFICE / FACTORY
    name: Mapped[str] = mapped_column(String(80))
    # Python weekday numbers of weekly rest days, Monday=0. "6" = Sunday.
    rest_weekdays: Mapped[str] = mapped_column(String(20), default="6")

    @property
    def rest_days(self) -> set[int]:
        return {int(x) for x in self.rest_weekdays.split(",") if x.strip()}


class Holiday(Timestamps, Base):
    __tablename__ = "holiday"
    __table_args__ = (UniqueConstraint("calendar_code", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    calendar_code: Mapped[str] = mapped_column(ForeignKey("calendar.code"))
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(120))


class ScanImport(Timestamps, Base):
    __tablename__ = "scan_import"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(200))
    imported_by: Mapped[Optional[str]] = mapped_column(String(160))
    lines_total: Mapped[int] = mapped_column(Integer, default=0)
    lines_new: Mapped[int] = mapped_column(Integer, default=0)
    unknown_users: Mapped[Optional[list]] = mapped_column(JSON)
    date_from: Mapped[Optional[date]] = mapped_column(Date)
    date_to: Mapped[Optional[date]] = mapped_column(Date)


class ScanPunch(Base):
    __tablename__ = "scan_punch"
    __table_args__ = (UniqueConstraint("scan_user", "punched_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_user: Mapped[str] = mapped_column(String(12))  # raw device ID, e.g. 0000002898
    employee_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    punched_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    import_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_import.id"))


class DailyAttendance(Timestamps, Base):
    __tablename__ = "daily_attendance"
    __table_args__ = (UniqueConstraint("employee_id", "work_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_code: Mapped[Optional[str]] = mapped_column(String(12))
    is_rest_day: Mapped[bool] = mapped_column(Boolean, default=False)
    first_in: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_out: Mapped[Optional[datetime]] = mapped_column(DateTime)
    scan_count: Mapped[int] = mapped_column(Integer, default=0)
    day_code: Mapped[Optional[str]] = mapped_column(String(2))
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)
    scan_ot_minutes: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(10), default="scan")  # scan / manual / leave

    employee: Mapped[Employee] = relationship()


OT_STATUSES = ["pending", "approved", "rejected", "verified", "cancelled", "paid"]


class OTRequest(Timestamps, Base):
    __tablename__ = "ot_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # OT-2026-000123
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    ot_from: Mapped[datetime] = mapped_column(DateTime)
    ot_to: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[Optional[str]] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(10), default="web")  # web / paper / import
    capture_line_id: Mapped[Optional[int]] = mapped_column(Integer)
    pre_approved: Mapped[bool] = mapped_column(Boolean, default=True)  # False = after the fact
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    day_kind: Mapped[str] = mapped_column(String(10), default="workday")  # workday / holiday
    requested_minutes: Mapped[int] = mapped_column(Integer, default=0)
    split: Mapped[Optional[dict]] = mapped_column(JSON)  # pay code -> minutes
    scan_minutes: Mapped[Optional[int]] = mapped_column(Integer)  # minutes covered by scans
    payable_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    flags: Mapped[Optional[list]] = mapped_column(JSON)  # [{code, level, msg}]
    override_note: Mapped[Optional[str]] = mapped_column(String(300))  # GM override, logged
    submitted_by: Mapped[Optional[str]] = mapped_column(String(160))
    approval_id: Mapped[Optional[int]] = mapped_column(ForeignKey("approval_request.id"))
    pay_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pay_batch.id"))

    employee: Mapped[Employee] = relationship()

    @property
    def requested_hours(self) -> float:
        return round(self.requested_minutes / 60, 2)


class PayBatch(Timestamps, Base):
    __tablename__ = "pay_batch"
    __table_args__ = (UniqueConstraint("cycle", "period_start", "batch_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle: Mapped[str] = mapped_column(String(16))  # STAFF_MONTHLY / OPERATOR_HALF
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    batch_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft/checked/sent/imported
    created_by: Mapped[Optional[str]] = mapped_column(String(160))
    checked_by: Mapped[Optional[str]] = mapped_column(String(160))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    bplus_status_note: Mapped[Optional[str]] = mapped_column(String(300))
    totals: Mapped[Optional[dict]] = mapped_column(JSON)
    flags: Mapped[Optional[list]] = mapped_column(JSON)

    lines: Mapped[list["PayBatchLine"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="PayBatchLine.employee_id"
    )


class PayBatchLine(Base):
    __tablename__ = "pay_batch_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("pay_batch.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"))
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20))
    pay_code: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[float] = mapped_column(Float)  # hours
    source_refs: Mapped[Optional[list]] = mapped_column(JSON)
    flag: Mapped[Optional[str]] = mapped_column(String(200))

    batch: Mapped[PayBatch] = relationship(back_populates="lines")
    employee: Mapped[Employee] = relationship()


class PayTotal(Timestamps, Base):
    """Totals read back from Bplus. No individual salary ever comes in."""

    __tablename__ = "pay_total"
    __table_args__ = (UniqueConstraint("month", "org_unit_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[date] = mapped_column(Date)  # first day of month
    org_unit_code: Mapped[str] = mapped_column(String(20))
    total_pay_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    ot_cost: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    headcount_paid: Mapped[Optional[int]] = mapped_column(Integer)
    ot_hours: Mapped[Optional[float]] = mapped_column(Float)


class OTBudget(Timestamps, Base):
    __tablename__ = "ot_budget"
    __table_args__ = (UniqueConstraint("month", "org_unit_code", "employee_group"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[date] = mapped_column(Date)
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20))  # None = company
    employee_group: Mapped[Optional[str]] = mapped_column(String(24))
    amount_thb: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    hours: Mapped[Optional[float]] = mapped_column(Float)
    note: Mapped[Optional[str]] = mapped_column(Text)
