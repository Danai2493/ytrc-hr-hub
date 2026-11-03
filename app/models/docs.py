"""Phase 6, documents and policies (blueprint M1 personnel file, M6 policy library).

Personnel documents are HR-only (PDPA): the employee sees the list of their own documents,
supervisors see nothing. Files are kept under STORAGE_DIR; a document can also be
"paper only" (the original stays in the personnel file cabinet) with no scan.
Policies have versions; a new version needs a new acknowledgement."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

# code: (label, has expiry, needed for whom: all / foreign / none)
DOC_TYPES = {
    "contract": ("Employment contract", False, "all"),
    "id_card": ("Thai ID card copy", True, "thai"),
    "house_reg": ("House registration copy", False, "thai"),
    "education": ("Education certificate", False, "none"),
    "passport": ("Passport", True, "foreign"),
    "visa": ("Visa", True, "foreign"),
    "work_permit": ("Work permit", True, "foreign"),
    "certificate": ("Licence or certificate", True, "none"),
    "medical": ("Medical check result", True, "none"),
    "bank_book": ("Bank book copy (for Bplus)", False, "none"),
    "other": ("Other", True, "none"),
}
APPLIES_TO = {"all": "Everyone", "Operator": "Operators", "Staff": "Staff", "Manager": "Managers"}
ACK_METHODS = {"web": "Read in the app", "briefing": "Briefing, signed on paper", "paper": "Signed paper copy"}


class EmployeeDocument(Timestamps, Base):
    __tablename__ = "employee_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(20))
    title: Mapped[Optional[str]] = mapped_column(String(160))
    number_tail: Mapped[Optional[str]] = mapped_column(String(8))  # last 4 characters only, to tell copies apart
    issued_on: Mapped[Optional[date]] = mapped_column(Date)
    expires_on: Mapped[Optional[date]] = mapped_column(Date, index=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(300))
    file_mime: Mapped[Optional[str]] = mapped_column(String(80))
    file_name: Mapped[Optional[str]] = mapped_column(String(160))
    paper_only: Mapped[bool] = mapped_column(Boolean, default=False)
    current: Mapped[bool] = mapped_column(Boolean, default=True)  # False once a newer copy of the same type replaced it
    note: Mapped[Optional[str]] = mapped_column(String(300))
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(160))

    employee: Mapped[Employee] = relationship()


class Policy(Timestamps, Base):
    __tablename__ = "policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), index=True)  # POL-HR-01
    version: Mapped[int] = mapped_column(Integer, default=1)
    title_en: Mapped[str] = mapped_column(String(160))
    title_th: Mapped[Optional[str]] = mapped_column(String(160))
    summary: Mapped[Optional[str]] = mapped_column(Text)  # the key points in plain words, shown before the file
    changes: Mapped[Optional[str]] = mapped_column(Text)  # what changed from the last version
    applies_to: Mapped[str] = mapped_column(String(10), default="all")
    effective_on: Mapped[date] = mapped_column(Date)
    ack_required: Mapped[bool] = mapped_column(Boolean, default=True)
    ack_days: Mapped[int] = mapped_column(Integer, default=14)
    file_path: Mapped[Optional[str]] = mapped_column(String(300))
    file_mime: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft / published / retired
    published_by: Mapped[Optional[str]] = mapped_column(String(160))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("code", "version"),)

    acks: Mapped[list["PolicyAck"]] = relationship(back_populates="policy", cascade="all, delete-orphan")


class PolicyAck(Base):
    __tablename__ = "policy_ack"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policy.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    on: Mapped[date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(10), default="web")
    recorded_by: Mapped[Optional[str]] = mapped_column(String(160))
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (UniqueConstraint("policy_id", "employee_id"),)

    policy: Mapped[Policy] = relationship(back_populates="acks")
    employee: Mapped[Employee] = relationship()
