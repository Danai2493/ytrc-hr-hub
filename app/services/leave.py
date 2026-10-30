"""Leave (blueprint M4): requests with checks, balances, approval, and the link
to attendance so an approved leave day is no longer counted as absent.

Defaults follow the Labour Protection Act B.E. 2541 (as amended 2019): sick
leave paid up to 30 working days a year with a medical certificate from 3 days,
personal business leave 3 paid days, vacation at least 6 days after one year of
service, maternity 98 days counted on the calendar. HR changes the numbers in
Admin when company policy is more generous."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApprovalRequest, DailyAttendance, Employee, LeaveRequest, LeaveType, OTRequest, User
from . import approvals
from .approvals import StepSpec
from .common import audit, get_setting, next_ref
from .timecalc import Finding, is_rest_day

ACTIVE = ("pending", "approved")

# code, English, Thai, day code, paid, days/year, calendar days, document from, min service, notice, absence, gender
DEFAULT_TYPES = [
    ("SICK", "Sick leave", "ลาป่วย", "s", True, 30, False, 3, 0, 0, True, None),
    ("PERS", "Personal business leave", "ลากิจ", "p", True, 3, False, None, 0, 1, True, None),
    ("VAC", "Vacation", "ลาพักร้อน", "v", True, None, False, None, 365, 3, False, None),
    ("MAT", "Maternity leave", "ลาคลอด", "m", True, 98, True, None, 0, 0, False, "F"),
    ("MIL", "Military service", "ลารับราชการทหาร", "l", True, 60, False, None, 0, 0, False, None),
    ("TRAIN", "Training leave", "ลาฝึกอบรม", "l", False, None, False, None, 0, 7, False, None),
    ("UNPAID", "Unpaid leave", "ลาไม่รับค่าจ้าง", "u", False, None, False, None, 0, 0, True, None),
]


def seed_types(db: Session) -> None:
    for i, (code, en, th, dc, paid, days, cal, doc, svc, notice, absn, g) in enumerate(DEFAULT_TYPES):
        if db.get(LeaveType, code) is None:
            db.add(LeaveType(code=code, name_en=en, name_th=th, day_code=dc, paid=paid, days_per_year=days,
                             count_calendar_days=cal, document_from_days=doc, min_service_days=svc,
                             notice_days=notice, counts_as_absence=absn, gender=g, sort_order=i))
    db.flush()


def types(db: Session) -> list[LeaveType]:
    return list(db.scalars(select(LeaveType).where(LeaveType.active.is_(True)).order_by(LeaveType.sort_order)))


def db_type(db: Session, code: str) -> Optional[LeaveType]:
    lt = db.get(LeaveType, code) if code else None
    return lt if lt is not None and lt.active else None


def vacation_entitlement(db: Session, emp: Employee, year: int) -> float:
    """Vacation days for the year from the service-years rule, e.g. "1:6,3:8,5:10"."""
    rule = str(get_setting(db, "vacation_days_rule") or "1:6")
    on = date(year, 12, 31)
    years = (on - emp.join_date).days / 365.25
    days = 0.0
    for part in rule.split(","):
        try:
            y, d = part.split(":")
            if years >= float(y):
                days = max(days, float(d))
        except ValueError:
            continue
    return days


def entitlement(db: Session, emp: Employee, lt: LeaveType, year: int) -> Optional[float]:
    if lt.code == "VAC":
        return vacation_entitlement(db, emp, year)
    return lt.days_per_year


def count_days(db: Session, emp: Employee, lt: LeaveType, d0: date, d1: date, part: str = "full") -> float:
    if d1 < d0:
        return 0
    if lt.count_calendar_days:
        return float((d1 - d0).days + 1)
    n = 0.0
    d = d0
    while d <= d1:
        if not is_rest_day(db, emp, d):
            n += 1
        d += timedelta(days=1)
    if d0 == d1 and part in ("am", "pm") and n:
        n = 0.5
    return n


def used_days(db: Session, emp_id: str, code: str, year: int, exclude_id: Optional[int] = None) -> float:
    q = select(LeaveRequest).where(
        LeaveRequest.employee_id == emp_id, LeaveRequest.leave_type == code, LeaveRequest.status.in_(ACTIVE),
        LeaveRequest.start_date <= date(year, 12, 31), LeaveRequest.end_date >= date(year, 1, 1),
    )
    return sum(r.days for r in db.scalars(q) if r.id != exclude_id)


@dataclass
class Balance:
    type: LeaveType
    entitled: Optional[float]
    used: float
    pending: float

    @property
    def left(self) -> Optional[float]:
        return None if self.entitled is None else round(self.entitled - self.used - self.pending, 2)


def balances(db: Session, emp: Employee, year: int) -> list[Balance]:
    out = []
    rows = db.scalars(select(LeaveRequest).where(
        LeaveRequest.employee_id == emp.employee_id, LeaveRequest.status.in_(ACTIVE),
        LeaveRequest.start_date <= date(year, 12, 31), LeaveRequest.end_date >= date(year, 1, 1),
    )).all()
    for lt in types(db):
        if lt.gender and emp.gender and lt.gender != emp.gender:
            continue
        mine = [r for r in rows if r.leave_type == lt.code]
        out.append(Balance(lt, entitlement(db, emp, lt, year),
                           sum(r.days for r in mine if r.status == "approved"),
                           sum(r.days for r in mine if r.status == "pending")))
    return out


def check(
    db: Session, emp: Employee, lt: LeaveType, d0: date, d1: date, part: str = "full",
    has_document: bool = False, exclude_id: Optional[int] = None, today: Optional[date] = None,
) -> tuple[list[Finding], float]:
    today = today or date.today()
    f: list[Finding] = []
    if d1 < d0:
        return [Finding("leave_dates", "block", {})], 0
    if d0 < emp.join_date:
        f.append(Finding("before_join", "block", {"join": emp.join_date.isoformat()}))
    if emp.exit_date and d1 > emp.exit_date:
        f.append(Finding("after_exit", "block", {"exit": emp.exit_date.isoformat()}))
    if lt.gender and emp.gender and lt.gender != emp.gender:
        f.append(Finding("leave_not_for", "block", {"type": lt.name_en}))
    days = count_days(db, emp, lt, d0, d1, part)
    if days == 0:
        f.append(Finding("leave_no_workdays", "block", {}))
    clash = db.scalars(select(LeaveRequest).where(
        LeaveRequest.employee_id == emp.employee_id, LeaveRequest.status.in_(ACTIVE),
        LeaveRequest.start_date <= d1, LeaveRequest.end_date >= d0,
    )).all()
    for r in clash:
        if r.id == exclude_id:
            continue
        # two half days on the same date are fine
        if d0 == d1 == r.start_date == r.end_date and {part, r.part} == {"am", "pm"}:
            continue
        f.append(Finding("leave_overlap", "block", {"ref": r.ref}))
    years = {d0.year, d1.year}
    for y in years:
        ent = entitlement(db, emp, lt, y)
        if ent is None or not lt.paid:
            continue
        in_year = count_days(db, emp, lt, max(d0, date(y, 1, 1)), min(d1, date(y, 12, 31)), part)
        left = ent - used_days(db, emp.employee_id, lt.code, y, exclude_id)
        if in_year > left:
            f.append(Finding("leave_over_balance", "flag", {"days": round(in_year, 1), "left": round(max(left, 0), 1), "year": y}))
    if lt.min_service_days and (d0 - emp.join_date).days < lt.min_service_days:
        f.append(Finding("leave_service", "flag", {"days": lt.min_service_days}))
    if lt.document_from_days and days >= lt.document_from_days and not has_document:
        f.append(Finding("leave_document", "flag", {"days": lt.document_from_days}))
    if lt.notice_days and (d0 - today).days < lt.notice_days and d0 >= today:
        f.append(Finding("leave_notice", "flag", {"days": lt.notice_days}))
    if d0 < today:
        f.append(Finding("leave_after", "info", {}))
    ot = db.scalars(select(OTRequest).where(
        OTRequest.employee_id == emp.employee_id, OTRequest.work_date >= d0, OTRequest.work_date <= d1,
        OTRequest.status.in_(("pending", "approved", "verified", "paid")),
    )).all()
    for r in ot:
        f.append(Finding("leave_ot_same_day", "flag", {"ref": r.ref, "date": r.work_date.isoformat()}))
    if part == "full":
        worked = db.scalars(select(DailyAttendance.work_date).where(
            DailyAttendance.employee_id == emp.employee_id, DailyAttendance.work_date >= d0,
            DailyAttendance.work_date <= d1, DailyAttendance.day_code == "1",
        )).all()
        if worked:
            f.append(Finding("leave_scanned", "flag", {"dates": ", ".join(d.strftime("%d/%m") for d in worked[:5])}))
    return f, days


@dataclass
class LeaveResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    request: Optional[LeaveRequest] = None
    days: float = 0


def create(
    db: Session, user: User, emp: Employee, code: str, d0: date, d1: date, part: str = "full",
    reason: str = "", has_document: bool = False, source: str = "web", capture_id: Optional[int] = None,
) -> LeaveResult:
    lt = db.get(LeaveType, code)
    if lt is None or not lt.active:
        return LeaveResult(False, [Finding("leave_type", "block", {})])
    if d0 != d1:
        part = "full"
    findings, days = check(db, emp, lt, d0, d1, part, has_document)
    if any(x.level == "block" for x in findings):
        return LeaveResult(False, findings, None, days)
    req = LeaveRequest(
        ref=next_ref(db, "LV", d0.year), employee_id=emp.employee_id, leave_type=code, start_date=d0, end_date=d1,
        part=part, days=days, reason=(reason or "")[:300], has_document=has_document, source=source,
        capture_id=capture_id, flags=[x.as_dict() for x in findings], submitted_by=user.label, status="pending",
    )
    req.employee, req.type = emp, lt
    db.add(req)
    db.flush()
    steps = [StepSpec("dept_head", head_of=emp.org_unit_code)]
    if any(x.level == "flag" and x.code in ("leave_document", "leave_over_balance", "leave_service") for x in findings):
        steps.append(StepSpec("hr_check", role="hr"))
    span = d0.strftime("%d/%m") + ("" if d0 == d1 else "–" + d1.strftime("%d/%m/%Y"))
    appr = approvals.submit(
        db, request_type="LEAVE", entity="leave_request", entity_id=str(req.id),
        title=f"{lt.name_en} {emp.employee_id} {span} ({days:g} d)", steps=steps, user=user,
        org_unit_code=emp.org_unit_code, employee_id=emp.employee_id,
    )
    req.approval_id = appr.id
    audit(db, user, "create", "leave_request", req.ref, appr.title)
    return LeaveResult(True, findings, req, days)


def _refresh_attendance(db: Session, req: LeaveRequest) -> None:
    from .facescan import rebuild_attendance

    end = min(req.end_date, date.today())
    if req.start_date <= end:
        rebuild_attendance(db, req.start_date, end, [req.employee_id])


@approvals.on_close("LEAVE")
def _leave_close(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    req = db.get(LeaveRequest, int(appr.entity_id))
    if req is None:
        return
    req.status = {"approved": "approved", "rejected": "rejected", "cancelled": "cancelled"}[outcome]
    db.flush()  # the session does not autoflush; the rebuild must see the new status
    _refresh_attendance(db, req)


def approved_leave_codes(db: Session, d0: date, d1: date) -> dict[tuple[str, date], str]:
    """(employee, date) -> time sheet code for approved full-day leave. Used by
    the attendance rebuild so leave days stop showing as absent."""
    rows = db.scalars(select(LeaveRequest).where(
        LeaveRequest.status == "approved", LeaveRequest.start_date <= d1, LeaveRequest.end_date >= d0,
    )).all()
    out: dict[tuple[str, date], str] = {}
    for r in rows:
        if r.part != "full":
            continue  # a half day still works the other half
        d = max(r.start_date, d0)
        while d <= min(r.end_date, d1):
            out[(r.employee_id, d)] = r.type.day_code
            d += timedelta(days=1)
    return out


def leave_by_type(db: Session, emp_ids: Optional[set[str]], d0: date, d1: date) -> dict[str, float]:
    """Approved leave days by type in a period (days falling inside the period)."""
    rows = db.scalars(select(LeaveRequest).where(
        LeaveRequest.status == "approved", LeaveRequest.start_date <= d1, LeaveRequest.end_date >= d0,
    )).all()
    out: dict[str, float] = {}
    for r in rows:
        if emp_ids is not None and r.employee_id not in emp_ids:
            continue
        if r.start_date >= d0 and r.end_date <= d1:
            n = r.days
        else:
            n = count_days(db, r.employee, r.type, max(r.start_date, d0), min(r.end_date, d1), r.part)
        out[r.leave_type] = out.get(r.leave_type, 0) + n
    return out
