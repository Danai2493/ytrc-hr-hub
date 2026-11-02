"""Pulse surveys (blueprint M14): short, anonymous, a few times a year.

Anonymity by design: a response stores only the department (top-level unit), the
employee group and the date. Who has answered is kept apart (PulseInvite.done) with no
link to the answers. Results for any group under 5 answers are hidden, and written
comments are shown to HR only."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, Timestamps

MIN_GROUP = 5
# Our own questions (1 = strongly disagree ... 5 = strongly agree), plus one comment
DEFAULT_QUESTIONS = [
    {"id": "clear", "en": "I know clearly what my job needs from me this month.", "th": "ฉันรู้ชัดเจนว่างานเดือนนี้ต้องการอะไรจากฉัน"},
    {"id": "tools", "en": "I have the tools, materials and information I need.", "th": "ฉันมีเครื่องมือ วัสดุ และข้อมูลที่ต้องใช้ทำงานครบ"},
    {"id": "fair", "en": "My supervisor treats people fairly.", "th": "หัวหน้าของฉันปฏิบัติต่อทุกคนอย่างเป็นธรรม"},
    {"id": "safe", "en": "I feel safe at work.", "th": "ฉันรู้สึกปลอดภัยในการทำงาน"},
    {"id": "noticed", "en": "When I do good work, someone notices.", "th": "เมื่อฉันทำงานได้ดี มีคนเห็นและชื่นชม"},
    {"id": "ot", "en": "OT and shifts are shared fairly in my team.", "th": "OT และกะงานในทีมของฉันแบ่งกันอย่างเป็นธรรม"},
    {"id": "grow", "en": "I can see how to grow here.", "th": "ฉันมองเห็นทางเติบโตในที่นี่"},
    {"id": "recommend", "en": "I would tell a friend this is a good place to work.", "th": "ฉันจะบอกเพื่อนว่าที่นี่เป็นที่ทำงานที่ดี"},
]
COMMENT = {"en": "One thing we should fix first (optional):", "th": "เรื่องแรกที่ควรแก้ไข (ไม่บังคับ):"}
SCALE = {1: "Strongly disagree", 2: "Disagree", 3: "Not sure", 4: "Agree", 5: "Strongly agree"}


class PulseSurvey(Timestamps, Base):
    __tablename__ = "pulse_survey"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(80))
    opens_on: Mapped[date] = mapped_column(Date)
    closes_on: Mapped[date] = mapped_column(Date)
    groups: Mapped[list] = mapped_column(JSON, default=list)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open / closed
    created_by: Mapped[Optional[str]] = mapped_column(String(160))


class PulseInvite(Base):
    """Who is invited and whether they answered. Never linked to a response."""

    __tablename__ = "pulse_invite"
    __table_args__ = (UniqueConstraint("survey_id", "employee_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("pulse_survey.id"), index=True)
    employee_id: Mapped[str] = mapped_column(String(10), index=True)
    dept_code: Mapped[Optional[str]] = mapped_column(String(20))
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class PulseResponse(Base):
    """One anonymous answer sheet: no employee, no time of day."""

    __tablename__ = "pulse_response"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("pulse_survey.id"), index=True)
    dept_code: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    employee_group: Mapped[Optional[str]] = mapped_column(String(30))
    answers: Mapped[dict] = mapped_column(JSON, default=dict)  # question id -> 1-5
    comment: Mapped[Optional[str]] = mapped_column(String(500))
    on: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(6), default="web")  # web / paper
