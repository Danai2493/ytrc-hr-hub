"""Recruitment (blueprint M4): manpower requests, candidates, applications, interviews.

Pay is never stored here. Offers record the start date, job and grade; the
salary is agreed in the contract and keyed into Bplus only."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee, Job, OrgUnit, Position

REQ_REASONS = {"replacement": "Replacement", "new": "New headcount", "temporary": "Temporary or project"}
REQ_STATUSES = ["pending", "open", "on_hold", "filled", "cancelled", "rejected"]
SOURCES = {
    "walk_in": "Walk-in",
    "referral": "Employee referral",
    "facebook": "Facebook or LINE",
    "job_board": "Job website",
    "school": "School or university",
    "agency": "Agency or MOU",
    "website": "Company website",
    "rehire": "Former employee",
    "other": "Other",
}
STAGES = {
    "applied": "Applied",
    "screen": "Screening",
    "interview": "Interview",
    "offer": "Offer",
    "hired": "Hired",
    "rejected": "Not selected",
    "withdrawn": "Withdrew",
}
ACTIVE_STAGES = ("applied", "screen", "interview", "offer")
REJECT_REASONS = {
    "skills": "Skills or experience do not match",
    "interview": "Interview result",
    "legal": "Legal requirement (age, permit, documents)",
    "no_show": "Did not come to interview",
    "filled": "Position filled by another candidate",
    "rehire": "Not eligible for rehire",
    "other": "Other",
}
WITHDRAW_REASONS = {"pay": "Pay or benefits", "other_job": "Took another job", "location": "Travel or location", "shift": "Shift or hours", "personal": "Personal", "other": "Other"}
RECOMMEND = {"hire": "Recommend hire", "maybe": "Hold or second interview", "no": "Do not recommend"}
INTERVIEW_KINDS = {"phone": "Phone screen", "onsite": "Interview", "practical": "Practical test"}


class Requisition(Timestamps, Base):
    """A manpower request: a Department Head asks for people for a position."""

    __tablename__ = "requisition"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # MR-2026-000003
    org_unit_code: Mapped[str] = mapped_column(ForeignKey("org_unit.code"), index=True)
    position_id: Mapped[Optional[str]] = mapped_column(ForeignKey("position.position_id"))
    job_code: Mapped[str] = mapped_column(ForeignKey("job.code"))
    grade_code: Mapped[Optional[str]] = mapped_column(String(10))
    employee_group: Mapped[str] = mapped_column(String(24))
    headcount: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(String(12))  # replacement / new / temporary
    replaces_employee_id: Mapped[Optional[str]] = mapped_column(String(10))
    needed_by: Mapped[date] = mapped_column(Date)
    within_budget: Mapped[bool] = mapped_column(Boolean, default=True)  # seats free on the position when asked
    requirements: Mapped[Optional[str]] = mapped_column(Text)  # what the Department Head adds to the JD
    note: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    approval_id: Mapped[Optional[int]] = mapped_column(Integer)
    requested_by: Mapped[Optional[str]] = mapped_column(String(160))
    opened_on: Mapped[Optional[date]] = mapped_column(Date)
    closed_on: Mapped[Optional[date]] = mapped_column(Date)
    ad_th: Mapped[Optional[str]] = mapped_column(Text)
    ad_en: Mapped[Optional[str]] = mapped_column(Text)
    ad_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    channels: Mapped[list] = mapped_column(JSON, default=list)  # where the ad is posted
    hired: Mapped[int] = mapped_column(Integer, default=0)

    org_unit: Mapped[OrgUnit] = relationship()
    position: Mapped[Optional[Position]] = relationship()
    job: Mapped[Job] = relationship()
    applications: Mapped[list["Application"]] = relationship(back_populates="requisition", order_by="Application.id")

    @property
    def open_seats(self) -> int:
        return max(self.headcount - (self.hired or 0), 0)


class Candidate(Timestamps, Base):
    """A person who applied. Personal data is kept only as long as the PDPA notice says."""

    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)  # CA-2026-000041
    title: Mapped[Optional[str]] = mapped_column(String(8))
    gender: Mapped[Optional[str]] = mapped_column(String(1))
    full_name_en: Mapped[str] = mapped_column(String(120))
    full_name_th: Mapped[Optional[str]] = mapped_column(String(120))
    nickname: Mapped[Optional[str]] = mapped_column(String(40))
    nationality: Mapped[str] = mapped_column(String(20), default="Thai")
    birth_date: Mapped[Optional[date]] = mapped_column(Date)
    phone: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(160))
    line_id: Mapped[Optional[str]] = mapped_column(String(60))
    id_card_no: Mapped[Optional[str]] = mapped_column(String(20), index=True)  # Thai ID or passport
    home_area: Mapped[Optional[str]] = mapped_column(String(120))  # district and province only
    education: Mapped[Optional[str]] = mapped_column(String(120))
    experience_years: Mapped[Optional[int]] = mapped_column(Integer)
    experience: Mapped[Optional[str]] = mapped_column(Text)
    skills: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(12), default="walk_in")
    referred_by: Mapped[Optional[str]] = mapped_column(String(10))  # employee ID
    previous_employee_id: Mapped[Optional[str]] = mapped_column(String(10))
    consent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    consent_how: Mapped[Optional[str]] = mapped_column(String(20))  # paper / web / verbal
    talent_pool: Mapped[bool] = mapped_column(Boolean, default=False)  # agreed to be kept for future jobs
    retain_until: Mapped[Optional[date]] = mapped_column(Date, index=True)
    erased_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    file_path: Mapped[Optional[str]] = mapped_column(String(300))  # CV or application form photo
    file_mime: Mapped[Optional[str]] = mapped_column(String(60))
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_confidence: Mapped[Optional[dict]] = mapped_column(JSON)  # field -> confidence from the AI read
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(160))
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)

    applications: Mapped[list["Application"]] = relationship(back_populates="candidate", order_by="Application.id")

    def age_on(self, d: date) -> Optional[int]:
        if not self.birth_date:
            return None
        b = self.birth_date
        return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


class Application(Timestamps, Base):
    """One candidate for one manpower request, moving through the stages."""

    __tablename__ = "application"
    __table_args__ = (UniqueConstraint("requisition_id", "candidate_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requisition_id: Mapped[int] = mapped_column(ForeignKey("requisition.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    stage: Mapped[str] = mapped_column(String(10), default="applied", index=True)
    stage_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    applied_on: Mapped[date] = mapped_column(Date, default=date.today)
    checks: Mapped[list] = mapped_column(JSON, default=list)  # screening findings [{level, code, params}]
    close_reason: Mapped[Optional[str]] = mapped_column(String(12))
    close_note: Mapped[Optional[str]] = mapped_column(String(300))
    start_date: Mapped[Optional[date]] = mapped_column(Date)  # offer
    offer_note: Mapped[Optional[str]] = mapped_column(String(300))
    offer_status: Mapped[Optional[str]] = mapped_column(String(10))  # pending / accepted / declined
    offer_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    hire_approval_id: Mapped[Optional[int]] = mapped_column(Integer)
    employee_id: Mapped[Optional[str]] = mapped_column(String(10))  # once hired
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{stage, at, by, note}]

    requisition: Mapped[Requisition] = relationship(back_populates="applications")
    candidate: Mapped[Candidate] = relationship(back_populates="applications")
    interviews: Mapped[list["Interview"]] = relationship(back_populates="application", order_by="Interview.when")


class Interview(Timestamps, Base):
    __tablename__ = "interview"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="onsite")
    when: Mapped[datetime] = mapped_column(DateTime, index=True)
    interviewer_employee_id: Mapped[Optional[str]] = mapped_column(ForeignKey("employee.employee_id"))
    interviewer: Mapped[str] = mapped_column(String(160))
    questions: Mapped[list] = mapped_column(JSON, default=list)  # [{criterion, text}]
    scores: Mapped[Optional[dict]] = mapped_column(JSON)  # criterion -> 1-5
    evidence: Mapped[Optional[dict]] = mapped_column(JSON)  # criterion -> what the candidate said or did
    recommendation: Mapped[Optional[str]] = mapped_column(String(8))
    note: Mapped[Optional[str]] = mapped_column(Text)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ai_questions: Mapped[bool] = mapped_column(Boolean, default=False)

    application: Mapped[Application] = relationship(back_populates="interviews")
    interviewer_employee: Mapped[Optional[Employee]] = relationship()
