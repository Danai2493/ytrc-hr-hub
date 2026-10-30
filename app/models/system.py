"""Cross-cutting tables: users and roles, the approval engine, data issues,
the audit log, and AI form capture."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps

# Roles. A user can hold several.
ROLES = {
    "admin": "System admin (IT): users, settings. Cannot approve HR requests.",
    "hr_manager": "HR Manager: everything in HR, including restricted data",
    "hr": "HR officer: people data, OT verify, pay batches, review queue",
    "gm": "General Manager: final approvals, overrides, all dashboards",
    "dept_head": "Department Head: approves for own units, sees own units",
    "supervisor": "Supervisor: submits for own team, sees own team",
    "employee": "Employee: own record and own requests",
    "auditor": "Read-only access to logs and dashboards",
}


class User(Timestamps, Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[Optional[str]] = mapped_column(String(160), unique=True)  # Google account
    username: Mapped[Optional[str]] = mapped_column(String(60), unique=True)  # app-only login
    password_hash: Mapped[Optional[str]] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(120))
    employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employee.employee_id"))
    roles: Mapped[list] = mapped_column(JSON, default=list)
    language: Mapped[Optional[str]] = mapped_column(String(4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime)

    scopes: Mapped[list["UserScope"]] = relationship(cascade="all, delete-orphan")

    def has(self, *roles: str) -> bool:
        return any(r in (self.roles or []) for r in roles)

    @property
    def label(self) -> str:
        return self.email or self.username or self.display_name


class UserScope(Base):
    """Extra org units a supervisor covers (Department Heads get theirs automatically)."""

    __tablename__ = "user_scope"
    __table_args__ = (UniqueConstraint("user_id", "org_unit_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"))
    org_unit_code: Mapped[str] = mapped_column(ForeignKey("org_unit.code"))


class ApprovalRequest(Timestamps, Base):
    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_type: Mapped[str] = mapped_column(String(20), index=True)  # OT, EMP_CHANGE, TRANSFER...
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20))
    employee_id: Mapped[Optional[str]] = mapped_column(String(10))
    payload: Mapped[Optional[dict]] = mapped_column(JSON)  # what will be applied on approval
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    submitted_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("app_user.id"))
    submitted_by: Mapped[Optional[str]] = mapped_column(String(160))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    steps: Mapped[list["ApprovalStep"]] = relationship(
        back_populates="request", order_by="ApprovalStep.seq", cascade="all, delete-orphan"
    )

    @property
    def active_step(self) -> Optional["ApprovalStep"]:
        for s in self.steps:
            if s.seq == self.current_step:
                return s
        return None


class ApprovalStep(Base):
    __tablename__ = "approval_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("approval_request.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(40))  # dept_head / hr_verify / gm ...
    approver_employee_id: Mapped[Optional[str]] = mapped_column(String(10))
    approver_role: Mapped[Optional[str]] = mapped_column(String(20))
    decision: Mapped[Optional[str]] = mapped_column(String(12))  # approved / rejected / skipped
    decided_by: Mapped[Optional[str]] = mapped_column(String(160))
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    comment: Mapped[Optional[str]] = mapped_column(String(500))

    request: Mapped[ApprovalRequest] = relationship(back_populates="steps")


class DataIssue(Timestamps, Base):
    __tablename__ = "data_issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_code: Mapped[str] = mapped_column(String(20), index=True)
    severity: Mapped[str] = mapped_column(String(8))  # block / flag / info
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    employee_id: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(String(400))
    owner_role: Mapped[Optional[str]] = mapped_column(String(20))
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open/fixed/ignored
    resolved_by: Mapped[Optional[str]] = mapped_column(String(160))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    resolution_note: Mapped[Optional[str]] = mapped_column(String(300))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer)
    user_label: Mapped[Optional[str]] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(40), index=True)  # create/update/approve/view_restricted/login...
    entity: Mapped[Optional[str]] = mapped_column(String(40))
    entity_id: Mapped[Optional[str]] = mapped_column(String(40))
    summary: Mapped[Optional[str]] = mapped_column(String(400))
    changes: Mapped[Optional[dict]] = mapped_column(JSON)
    ip: Mapped[Optional[str]] = mapped_column(String(64))


CAPTURE_TYPES = {
    "OT_FORM": "OT form FM-HRM-25",
    "WORK_PERMIT": "Work permit",
    "LEAVE_FORM": "Leave form",
}


class FormCapture(Timestamps, Base):
    __tablename__ = "form_capture"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    form_type: Mapped[str] = mapped_column(String(20))
    file_path: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(60))
    original_name: Mapped[Optional[str]] = mapped_column(String(200))
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(14), default="uploaded", index=True)
    # uploaded / extracted / needs_review / approved / rejected / failed
    provider: Mapped[Optional[str]] = mapped_column(String(30))
    model: Mapped[Optional[str]] = mapped_column(String(60))
    header: Mapped[Optional[dict]] = mapped_column(JSON)  # form-level fields
    header_confidence: Mapped[Optional[dict]] = mapped_column(JSON)
    raw_response: Mapped[Optional[dict]] = mapped_column(JSON)
    error: Mapped[Optional[str]] = mapped_column(Text)
    employee_id: Mapped[Optional[str]] = mapped_column(String(10))  # for single-person forms
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(160))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_note: Mapped[Optional[str]] = mapped_column(String(500))

    lines: Mapped[list["CaptureLine"]] = relationship(
        back_populates="capture", order_by="CaptureLine.line_no", cascade="all, delete-orphan"
    )


class CaptureLine(Base):
    __tablename__ = "capture_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capture_id: Mapped[int] = mapped_column(ForeignKey("form_capture.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    fields: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[Optional[dict]] = mapped_column(JSON)
    issues: Mapped[Optional[list]] = mapped_column(JSON)  # [{code, level, msg}]
    status: Mapped[str] = mapped_column(String(10), default="review")  # ok / review / accepted / dropped
    ot_request_id: Mapped[Optional[int]] = mapped_column(Integer)
    min_confidence: Mapped[Optional[float]] = mapped_column(Float)

    capture: Mapped[FormCapture] = relationship(back_populates="lines")
