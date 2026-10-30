"""Performance (blueprint M10): yearly cycle, company and unit goals, KPI library,
individual appraisals with self and supervisor review, calibration.

Ratings never hold pay. The export for the pay review carries the rating and the
guideline merit percentage only; the salary itself stays in Bplus."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee, OrgUnit

RATINGS = {
    5: ("A", "Outstanding"),
    4: ("B", "Exceeds expectations"),
    3: ("C", "Fully meets expectations"),
    2: ("D", "Partly meets expectations"),
    1: ("E", "Does not meet expectations"),
}
DIRECTIONS = {"up": "Higher is better", "down": "Lower is better", "exact": "Hit the target"}
CYCLE_STATUSES = ["goals", "midyear", "review", "calibration", "closed"]
APPRAISAL_STATUSES = {
    "goals": "Setting goals",
    "goals_agreed": "Goals agreed",
    "self": "Self review",
    "manager": "Supervisor review",
    "endorse": "Department Head endorsement",
    "calibration": "Calibration",
    "released": "Results released",
}
FORMS = {"staff": "KPI form (staff and managers)", "operator": "Operator form"}
OPERATOR_PARTS = {"team": "Unit goals", "attendance": "Attendance and discipline", "skills": "Skills (ILUO)", "behaviour": "Behaviour (competencies)"}
DATA_SOURCES = {
    "manual": "Entered by the owner",
    "attendance_rate": "App: attendance rate (%)",
    "absent_days": "App: absent days per person",
    "late_count": "App: late arrivals per person",
    "ot_hours": "App: OT hours per person per month",
    "turnover": "App: yearly turnover (%)",
    "training_hours": "App: training hours per person",
    "skill_coverage": "App: critical skills with enough U/O people (%)",
}


class KpiDefinition(Timestamps, Base):
    """One measure in the KPI library, reused by company, unit and personal goals."""

    __tablename__ = "kpi_definition"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)  # KPI-PRD-01
    name_en: Mapped[str] = mapped_column(String(160))
    name_th: Mapped[Optional[str]] = mapped_column(String(160))
    unit: Mapped[str] = mapped_column(String(20), default="%")  # %, count, days, hours, tons, THB/ton
    direction: Mapped[str] = mapped_column(String(6), default="up")
    how_measured: Mapped[Optional[str]] = mapped_column(Text)
    data_source: Mapped[str] = mapped_column(String(20), default="manual")
    perspective: Mapped[Optional[str]] = mapped_column(String(20))  # safety, quality, delivery, cost, people
    owner_unit: Mapped[Optional[str]] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PerformanceCycle(Timestamps, Base):
    __tablename__ = "performance_cycle"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60))  # "2027 performance year"
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    goals_due: Mapped[date] = mapped_column(Date)
    midyear_on: Mapped[Optional[date]] = mapped_column(Date)
    review_due: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="goals", index=True)
    groups: Mapped[list] = mapped_column(JSON, default=list)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)  # {"staff": {"kpi": 70, "comp": 30}, "operator": {"team": 30, ...}}
    guide: Mapped[dict] = mapped_column(JSON, default=dict)  # rating -> guideline share % for calibration
    merit_guide: Mapped[dict] = mapped_column(JSON, default=dict)  # rating -> guideline merit % (no salaries)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    released_by: Mapped[Optional[str]] = mapped_column(String(160))
    note: Mapped[Optional[str]] = mapped_column(String(300))


class Goal(Timestamps, Base):
    """A company or unit goal for the year. Personal KPIs can link to one."""

    __tablename__ = "goal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("performance_cycle.id"), index=True)
    level: Mapped[str] = mapped_column(String(8))  # company / unit
    org_unit_code: Mapped[Optional[str]] = mapped_column(ForeignKey("org_unit.code"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("goal.id"))
    kpi_code: Mapped[Optional[str]] = mapped_column(ForeignKey("kpi_definition.code"))
    title: Mapped[str] = mapped_column(String(200))
    target: Mapped[Optional[float]] = mapped_column(Float)
    baseline: Mapped[Optional[float]] = mapped_column(Float)
    actual: Mapped[Optional[float]] = mapped_column(Float)
    actual_on: Mapped[Optional[date]] = mapped_column(Date)
    weight: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[Optional[str]] = mapped_column(String(160))

    kpi: Mapped[Optional[KpiDefinition]] = relationship()
    org_unit: Mapped[Optional[OrgUnit]] = relationship()
    parent: Mapped[Optional["Goal"]] = relationship(remote_side="Goal.id")


class Appraisal(Timestamps, Base):
    __tablename__ = "appraisal"
    __table_args__ = (UniqueConstraint("cycle_id", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("performance_cycle.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    form: Mapped[str] = mapped_column(String(10), default="staff")
    status: Mapped[str] = mapped_column(String(12), default="goals", index=True)
    reviewer_employee_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)  # direct supervisor
    endorser_employee_id: Mapped[Optional[str]] = mapped_column(String(10))  # Department Head (second level)
    org_unit_code: Mapped[str] = mapped_column(String(20), index=True)  # unit when the cycle opened
    items: Mapped[list] = mapped_column(JSON, default=list)  # KPI items, see services.perform
    competencies: Mapped[dict] = mapped_column(JSON, default=dict)  # code -> {self, mgr, note}
    parts: Mapped[dict] = mapped_column(JSON, default=dict)  # operator form: part -> {suggested, score, note}
    facts: Mapped[dict] = mapped_column(JSON, default=dict)  # attendance, lateness, skills filled in by the app
    goals_agreed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    midyear_note: Mapped[Optional[str]] = mapped_column(Text)
    midyear_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    self_note: Mapped[Optional[str]] = mapped_column(Text)
    self_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mgr_note: Mapped[Optional[str]] = mapped_column(Text)
    mgr_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mgr_by: Mapped[Optional[str]] = mapped_column(String(160))
    endorse_note: Mapped[Optional[str]] = mapped_column(Text)
    endorse_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    endorse_by: Mapped[Optional[str]] = mapped_column(String(160))
    kpi_score: Mapped[Optional[float]] = mapped_column(Float)
    comp_score: Mapped[Optional[float]] = mapped_column(Float)
    overall: Mapped[Optional[float]] = mapped_column(Float)
    proposed_rating: Mapped[Optional[int]] = mapped_column(Integer)
    final_rating: Mapped[Optional[int]] = mapped_column(Integer)
    calibration_note: Mapped[Optional[str]] = mapped_column(String(300))
    calibrated_by: Mapped[Optional[str]] = mapped_column(String(160))
    ack_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ack_note: Mapped[Optional[str]] = mapped_column(Text)
    ack_agree: Mapped[Optional[bool]] = mapped_column(Boolean)
    plan_id: Mapped[Optional[int]] = mapped_column(Integer)  # development / improvement plan
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)

    employee: Mapped[Employee] = relationship()
    cycle: Mapped[PerformanceCycle] = relationship()
