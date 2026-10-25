"""Core tables (blueprint section 3): Org Unit, Job, Grade, Position, Employee,
Employment Event. Every other module links back to these by key."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps

EMPLOYEE_GROUPS = ["Manager", "Staff", "Japanese Management", "Operator"]
PAY_TYPES = ["Monthly", "Daily"]
NATIONALITIES = ["Thai", "Myanmar", "Japanese", "Other"]
TITLES = ["Mr.", "Ms.", "Mrs."]
GENDERS = ["M", "F"]
STATUSES = ["Active", "Resigned"]
TEAMS = ["A", "B"]
ORG_LEVELS = ["division", "department", "section", "team"]

EXIT_REASONS = [
    ("RES-PAY", "Resigned: pay and benefits"),
    ("RES-WORK", "Resigned: workload or shift"),
    ("RES-BOSS", "Resigned: supervisor or team"),
    ("RES-CAREER", "Resigned: career or study"),
    ("RES-FAMILY", "Resigned: family or moving home"),
    ("RES-HEALTH", "Resigned: health"),
    ("RES-OTHER", "Resigned: other"),
    ("CONTRACT", "Contract ended"),
    ("PROBATION", "Did not pass probation"),
    ("TERMINATED", "Terminated"),
    ("RETIRED", "Retired"),
    ("DECEASED", "Passed away"),
    ("TO-STAFF", "Transfer to staff (new ID)"),
    ("TO-GROUP", "Transfer to group company"),
    ("NOT-RECORDED", "Not recorded (old data)"),
]

EVENT_TYPES = [
    "hire",
    "probation_pass",
    "probation_fail",
    "transfer",
    "promotion",
    "demotion",
    "grade_change",
    "group_change",
    "data_correction",
    "exit",
    "rehire",
]


class OrgUnit(Timestamps, Base):
    __tablename__ = "org_unit"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(120))
    name_th: Mapped[Optional[str]] = mapped_column(String(120))
    level: Mapped[str] = mapped_column(String(12))  # division/department/section/team
    parent_code: Mapped[Optional[str]] = mapped_column(ForeignKey("org_unit.code"))
    head_employee_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("employee.employee_id", use_alter=True, name="fk_org_unit_head")
    )
    calendar_code: Mapped[str] = mapped_column(String(12), default="OFFICE")
    default_shift_code: Mapped[Optional[str]] = mapped_column(ForeignKey("shift.code"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent: Mapped[Optional["OrgUnit"]] = relationship(remote_side=[code], back_populates="children")
    children: Mapped[list["OrgUnit"]] = relationship(back_populates="parent")
    head: Mapped[Optional["Employee"]] = relationship(foreign_keys=[head_employee_id])

    def path(self) -> list["OrgUnit"]:
        out, node = [], self
        while node is not None:
            out.append(node)
            node = node.parent
        return list(reversed(out))

    @property
    def department(self) -> Optional["OrgUnit"]:
        for n in self.path():
            if n.level == "department":
                return n
        return None

    @property
    def division(self) -> Optional["OrgUnit"]:
        for n in self.path():
            if n.level == "division":
                return n
        return None


class Grade(Timestamps, Base):
    __tablename__ = "grade"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)  # e.g. MG, S-SV, Operator
    name: Mapped[str] = mapped_column(String(80))
    rank: Mapped[int] = mapped_column(Integer, default=0)  # higher = more senior
    # Salary band stays in Bplus / HR practice. Only the band label lives here.
    band_label: Mapped[Optional[str]] = mapped_column(String(40))


class Job(Timestamps, Base):
    __tablename__ = "job"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    title_en: Mapped[str] = mapped_column(String(120))
    title_th: Mapped[Optional[str]] = mapped_column(String(120))
    job_family: Mapped[Optional[str]] = mapped_column(String(60))
    default_grade_code: Mapped[Optional[str]] = mapped_column(ForeignKey("grade.code"))
    jd_url: Mapped[Optional[str]] = mapped_column(String(500))
    purpose: Mapped[Optional[str]] = mapped_column(Text)
    responsibilities: Mapped[Optional[str]] = mapped_column(Text)
    education: Mapped[Optional[str]] = mapped_column(String(200))
    experience: Mapped[Optional[str]] = mapped_column(String(200))
    next_job_code: Mapped[Optional[str]] = mapped_column(ForeignKey("job.code"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Position(Timestamps, Base):
    """A budgeted seat (or a group of identical seats for operator lines)."""

    __tablename__ = "position"

    position_id: Mapped[str] = mapped_column(String(20), primary_key=True)  # POS-0001
    title: Mapped[str] = mapped_column(String(120))
    org_unit_code: Mapped[str] = mapped_column(ForeignKey("org_unit.code"))
    job_code: Mapped[Optional[str]] = mapped_column(ForeignKey("job.code"))
    grade_code: Mapped[Optional[str]] = mapped_column(ForeignKey("grade.code"))
    seats: Mapped[int] = mapped_column(Integer, default=1)
    budgeted: Mapped[bool] = mapped_column(Boolean, default=True)
    budget_year: Mapped[Optional[int]] = mapped_column(Integer)
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    min_backups_uo: Mapped[int] = mapped_column(Integer, default=0)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    reports_to_position_id: Mapped[Optional[str]] = mapped_column(ForeignKey("position.position_id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    org_unit: Mapped[OrgUnit] = relationship()
    job: Mapped[Optional[Job]] = relationship()
    grade: Mapped[Optional[Grade]] = relationship()
    incumbents: Mapped[list["Employee"]] = relationship(
        primaryjoin="and_(Position.position_id==Employee.position_id, Employee.status=='Active')",
        viewonly=True,
    )

    @property
    def filled(self) -> int:
        return len(self.incumbents)

    @property
    def status(self) -> str:
        if self.frozen:
            return "frozen"
        if self.filled >= self.seats:
            return "filled" if self.filled == self.seats else "over"
        return "vacant"


class Employee(Timestamps, Base):
    __tablename__ = "employee"

    employee_id: Mapped[str] = mapped_column(String(10), primary_key=True)  # YTR009 / YTOP011
    employee_group: Mapped[str] = mapped_column(String(24))
    pay_type: Mapped[str] = mapped_column(String(10), default="Monthly")
    title: Mapped[Optional[str]] = mapped_column(String(8))
    gender: Mapped[Optional[str]] = mapped_column(String(1))
    full_name_en: Mapped[str] = mapped_column(String(120))
    full_name_th: Mapped[Optional[str]] = mapped_column(String(120))
    full_name_my: Mapped[Optional[str]] = mapped_column(String(120))
    nickname: Mapped[Optional[str]] = mapped_column(String(40))
    nationality: Mapped[str] = mapped_column(String(20), default="Thai")
    birth_date: Mapped[Optional[date]] = mapped_column(Date)
    join_date: Mapped[date] = mapped_column(Date)
    probation_end_date: Mapped[Optional[date]] = mapped_column(Date)
    contract_end_date: Mapped[Optional[date]] = mapped_column(Date)

    org_unit_code: Mapped[str] = mapped_column(ForeignKey("org_unit.code"))
    position_id: Mapped[Optional[str]] = mapped_column(ForeignKey("position.position_id"))
    job_code: Mapped[Optional[str]] = mapped_column(ForeignKey("job.code"))
    grade_code: Mapped[Optional[str]] = mapped_column(ForeignKey("grade.code"))
    position_since: Mapped[Optional[date]] = mapped_column(Date)
    team: Mapped[Optional[str]] = mapped_column(String(2))
    shift_code: Mapped[Optional[str]] = mapped_column(ForeignKey("shift.code"))
    # Per-person holiday calendar override (e.g. QMS follows Office).
    calendar_override: Mapped[Optional[str]] = mapped_column(String(12))

    status: Mapped[str] = mapped_column(String(10), default="Active")
    exit_date: Mapped[Optional[date]] = mapped_column(Date)
    exit_reason_code: Mapped[Optional[str]] = mapped_column(String(20))
    previous_employee_id: Mapped[Optional[str]] = mapped_column(String(10))
    work_email: Mapped[Optional[str]] = mapped_column(String(160), unique=True)
    remark: Mapped[Optional[str]] = mapped_column(Text)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    org_unit: Mapped[OrgUnit] = relationship(foreign_keys=[org_unit_code])
    position: Mapped[Optional[Position]] = relationship(foreign_keys=[position_id])
    job: Mapped[Optional[Job]] = relationship()
    grade: Mapped[Optional[Grade]] = relationship()
    restricted: Mapped[Optional["EmployeeRestricted"]] = relationship(
        back_populates="employee", uselist=False
    )
    events: Mapped[list["EmploymentEvent"]] = relationship(
        back_populates="employee", order_by="EmploymentEvent.effective_date"
    )

    def age_on(self, d: date) -> Optional[int]:
        if not self.birth_date:
            return None
        b = self.birth_date
        return d.year - b.year - ((d.month, d.day) < (b.month, b.day))

    def is_active_on(self, d: date) -> bool:
        if d < self.join_date:
            return False
        return self.exit_date is None or d <= self.exit_date

    @property
    def calendar_code(self) -> str:
        return self.calendar_override or (self.org_unit.calendar_code if self.org_unit else "OFFICE")

    @property
    def display_name(self) -> str:
        return self.full_name_en or self.employee_id

    @property
    def is_foreign_worker(self) -> bool:
        return self.nationality not in ("Thai", "Japanese")


class EmployeeRestricted(Timestamps, Base):
    """HR-only data. Every view is written to the access log (PDPA)."""

    __tablename__ = "employee_restricted"

    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), primary_key=True)
    id_card_no: Mapped[Optional[str]] = mapped_column(String(20))
    passport_no: Mapped[Optional[str]] = mapped_column(String(20))
    work_permit_no: Mapped[Optional[str]] = mapped_column(String(30))
    work_permit_expiry: Mapped[Optional[date]] = mapped_column(Date)
    visa_expiry: Mapped[Optional[date]] = mapped_column(Date)
    disability: Mapped[Optional[str]] = mapped_column(String(120))
    residence_room: Mapped[Optional[str]] = mapped_column(String(20))

    employee: Mapped[Employee] = relationship(back_populates="restricted")


class EmploymentEvent(Timestamps, Base):
    """History, not overwrite: every change to a person's job is a dated event."""

    __tablename__ = "employment_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(20))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    from_org_unit: Mapped[Optional[str]] = mapped_column(String(20))
    to_org_unit: Mapped[Optional[str]] = mapped_column(String(20))
    from_position: Mapped[Optional[str]] = mapped_column(String(20))
    to_position: Mapped[Optional[str]] = mapped_column(String(20))
    from_job: Mapped[Optional[str]] = mapped_column(String(20))
    to_job: Mapped[Optional[str]] = mapped_column(String(20))
    from_grade: Mapped[Optional[str]] = mapped_column(String(20))
    to_grade: Mapped[Optional[str]] = mapped_column(String(20))
    from_group: Mapped[Optional[str]] = mapped_column(String(24))
    to_group: Mapped[Optional[str]] = mapped_column(String(24))
    reason_code: Mapped[Optional[str]] = mapped_column(String(20))
    note: Mapped[Optional[str]] = mapped_column(Text)
    changes: Mapped[Optional[dict]] = mapped_column(JSON)  # field -> [old, new] for corrections
    approval_id: Mapped[Optional[int]] = mapped_column(ForeignKey("approval_request.id"))
    created_by: Mapped[Optional[str]] = mapped_column(String(160))
    # Future-dated events wait until their date, then update the employee record.
    applied: Mapped[bool] = mapped_column(Boolean, default=False)

    employee: Mapped[Employee] = relationship(back_populates="events")


class Setting(Timestamps, Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JSON)
    description: Mapped[Optional[str]] = mapped_column(String(300))


class Sequence(Base):
    """Readable running numbers (OT-2026-000123) that work on every database."""

    __tablename__ = "sequence"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(40))
    value: Mapped[int] = mapped_column(Integer, default=0)
