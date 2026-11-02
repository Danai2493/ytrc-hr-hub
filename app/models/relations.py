"""Employee relations (blueprint M6): discipline, grievances and harassment complaints.

Thai Labour Protection Act points the app checks (HR and a lawyer stay responsible):
- s.119(4): a written warning is in force for one year from the date of the offence;
  repeating the same offence in that year can allow dismissal without severance.
- s.108: suspension without pay as a penalty only when the work rules list it (the app
  keeps it to 1-7 days). Suspension DURING an investigation (s.116-117: written order,
  max 7 days, at least 50% of the wage) is a different thing and is handled outside this case.
- s.17/1: a dismissal letter must state the reasons; reasons not written cannot be used later.
- s.16: sexual harassment complaints are confidential and handled by the HR Manager.
- s.108: the work rules give a grievance route; the app tracks the reply date.
Everyone gets to explain their side before a decision."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

KINDS = {"discipline": "Discipline", "grievance": "Grievance (complaint by an employee)", "harassment": "Harassment complaint"}
CATEGORIES = {
    "attendance": "Attendance, lateness, absence without leave",
    "safety": "Safety rules",
    "conduct": "Conduct, fighting, insubordination",
    "quality": "Work quality, negligence",
    "fraud": "Dishonesty, theft, fraud",
    "alcohol": "Alcohol or drugs at work",
    "workload": "Workload, shifts, OT",
    "treatment": "Treatment by a supervisor or colleague",
    "pay": "Pay, benefits (checked in Bplus)",
    "harassment": "Sexual or other harassment",
    "other": "Other",
}
OUTCOMES = {
    "discipline": {"none": "No action: not proven", "coaching": "Coaching talk (no warning)", "verbal": "Verbal warning (recorded)",
                   "written": "Written warning", "suspension": "Suspension without pay (penalty in the work rules)", "dismissal": "Dismissal"},
    "grievance": {"upheld": "Upheld: action agreed", "partly": "Partly upheld", "not_upheld": "Not upheld, explained to the employee"},
    "harassment": {"upheld": "Upheld: action agreed", "partly": "Partly upheld", "not_upheld": "Not upheld, explained to the employee"},
}
STATUSES = {"open": "Open", "investigating": "Investigating", "decision": "Waiting for decision", "closed": "Closed"}
# who must decide each outcome (the person who proposes can never decide their own case)
DECIDER = {"written": ("hr_manager",), "suspension": ("hr_manager",), "dismissal": ("gm",)}
SERIOUS = {"fraud", "alcohol"}  # LPA s.119: may not need an earlier warning


class ErCase(Timestamps, Base):
    __tablename__ = "er_case"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # ER-2026-000003
    kind: Mapped[str] = mapped_column(String(12), index=True)
    category: Mapped[str] = mapped_column(String(12))
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)  # discipline: the employee; complaint: who raised it
    other_employee_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)  # complaint: the person complained about
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    incident_on: Mapped[date] = mapped_column(Date)
    reported_on: Mapped[date] = mapped_column(Date)
    reported_by: Mapped[Optional[str]] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(14), default="open", index=True)
    reply_due: Mapped[Optional[date]] = mapped_column(Date)  # complaints: answer the employee by
    statement: Mapped[Optional[str]] = mapped_column(Text)  # the employee's own explanation
    statement_on: Mapped[Optional[date]] = mapped_column(Date)
    findings: Mapped[Optional[str]] = mapped_column(Text)
    outcome: Mapped[Optional[str]] = mapped_column(String(12))
    reasons: Mapped[Optional[str]] = mapped_column(Text)  # written reasons (needed for warnings, suspension, dismissal)
    suspension_from: Mapped[Optional[date]] = mapped_column(Date)
    suspension_days: Mapped[Optional[int]] = mapped_column(Integer)
    suspension_pay_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    warning_until: Mapped[Optional[date]] = mapped_column(Date)  # written warning in force until
    proposed_by: Mapped[Optional[str]] = mapped_column(String(160))
    proposed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    decided_by: Mapped[Optional[str]] = mapped_column(String(160))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    letter_given_on: Mapped[Optional[date]] = mapped_column(Date)
    employee_signed: Mapped[Optional[bool]] = mapped_column(Boolean)  # False = refused to sign (two witnesses then)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    log: Mapped[list] = mapped_column(JSON, default=list)  # [{at, by, text}]

    employee: Mapped[Employee] = relationship()
