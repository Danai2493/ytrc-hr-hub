"""Talent review (blueprint M11): the yearly 9-box review.

Performance comes from the released appraisal rating. Potential is a separate,
forward-looking judgement made by the supervisor and agreed in the talent review
meeting. Growth Compass scores are NOT used here: that tool is for development only.
The review is confidential (HR, GM and the person's own management line) and is never
used for pay."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, Timestamps
from .core import Employee

# 1 = low, 2 = medium, 3 = high
LEVELS = {1: "Low", 2: "Medium", 3: "High"}
POTENTIAL_QUESTIONS = {
    "learn": "Learns new work quickly and uses it without being told",
    "scope": "Wants, and can handle, bigger or harder work",
    "lead": "Others follow them; they help the team do better",
}
# (performance, potential) -> (box number, name, suggested action)
BOXES = {
    (3, 3): (9, "Future leader", "Stretch role or project, name as a successor, mentor from a senior leader"),
    (3, 2): (8, "High performer", "Grow in the role, add a stretch task, check the next step in 1-2 years"),
    (3, 1): (7, "Trusted expert", "Keep and recognise, use as a trainer or coach for others"),
    (2, 3): (6, "Growing talent", "Coach on the gaps in results, give new experience"),
    (2, 2): (5, "Core contributor", "Keep developing in the role with a clear development plan"),
    (2, 1): (4, "Steady contributor", "Keep skills current, recognise reliability"),
    (1, 3): (3, "Wrong fit or new in role", "Find the cause: role fit, support, or too new; review in 6 months"),
    (1, 2): (2, "Needs support", "Clear goals and close coaching for the next 6 months"),
    (1, 1): (1, "Act now", "Improvement plan with clear goals and dates, then decide"),
}
RISKS = {"low": "Low", "medium": "Medium", "high": "High"}
NEXT_MOVES = {
    "stay": "Stay and grow in the role",
    "ready_2y": "Ready for a bigger role in 1-2 years",
    "ready_now": "Ready for a bigger role now",
    "move": "Better in a different role",
}
STATUSES = {"draft": "Draft", "proposed": "Proposed", "agreed": "Agreed in review"}


def perf_level(rating: Optional[int]) -> Optional[int]:
    """A-E appraisal rating to the 9-box performance axis: A/B high, C medium, D/E low."""
    if rating is None:
        return None
    return 3 if rating >= 4 else 2 if rating == 3 else 1


def box_of(perf: Optional[int], pot: Optional[int]):
    return BOXES.get((perf, pot)) if perf and pot else None


class TalentReview(Timestamps, Base):
    __tablename__ = "talent_review"
    __table_args__ = (UniqueConstraint("year", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employee.employee_id"), index=True)
    org_unit_code: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    appraisal_id: Mapped[Optional[int]] = mapped_column(Integer)  # rating used for the performance axis
    rating: Mapped[Optional[int]] = mapped_column(Integer)
    performance: Mapped[Optional[int]] = mapped_column(Integer)  # 1-3
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # potential question -> 1-3
    potential: Mapped[Optional[int]] = mapped_column(Integer)  # 1-3
    box: Mapped[Optional[int]] = mapped_column(Integer, index=True)  # 1-9
    risk: Mapped[Optional[str]] = mapped_column(String(8))  # retention risk
    risk_reason: Mapped[Optional[str]] = mapped_column(String(300))
    next_move: Mapped[Optional[str]] = mapped_column(String(10))
    action: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="draft", index=True)
    proposed_by: Mapped[Optional[str]] = mapped_column(String(160))
    proposed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    agreed_by: Mapped[Optional[str]] = mapped_column(String(160))
    agreed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    review_note: Mapped[Optional[str]] = mapped_column(String(300))

    employee: Mapped[Employee] = relationship()
