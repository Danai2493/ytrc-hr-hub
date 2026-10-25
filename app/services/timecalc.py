"""Shift, calendar and OT rules. These replace the formulas of the step 3 OT
workbook (OT_Time_Data_v2.xlsx) and run when data is entered."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import Calendar, DailyAttendance, Employee, Holiday, OTRequest, Shift
from .common import get_setting

ACTIVE_OT = ("pending", "approved", "verified", "paid")
NIGHT_START = time(22, 0)
NIGHT_END = time(6, 0)


@dataclass
class Finding:
    code: str
    level: str  # block / flag / info
    params: dict

    def as_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "params": self.params}


def week_bounds(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def shift_for(db: Session, emp: Employee, d: date) -> Optional[Shift]:
    code = emp.shift_code or (emp.org_unit.default_shift_code if emp.org_unit else None) or "OFFICE"
    return db.get(Shift, code)


def shift_window(shift: Shift, d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, shift.start)
    end = datetime.combine(d, shift.end)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def is_rest_day(db: Session, emp: Employee, d: date) -> bool:
    cal = db.get(Calendar, emp.calendar_code)
    if cal and d.weekday() in cal.rest_days:
        return True
    return (
        db.scalar(select(Holiday.id).where(Holiday.calendar_code == emp.calendar_code, Holiday.day == d))
        is not None
    )


def holiday_name(db: Session, emp: Employee, d: date) -> Optional[str]:
    return db.scalar(
        select(Holiday.name).where(Holiday.calendar_code == emp.calendar_code, Holiday.day == d)
    )


def overlap_minutes(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> int:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0, int((hi - lo).total_seconds() // 60))


def combine_ot_times(d: date, t_from: time, t_to: time, shift: Optional[Shift]) -> tuple[datetime, datetime]:
    """Turn the HH:MM on a form into real date-times. Times after midnight on a
    night shift (or OT that runs past midnight) move to the next day."""
    f = datetime.combine(d, t_from)
    t = datetime.combine(d, t_to)
    if shift is not None and shift.is_night and t_from < time(12, 0):
        # e.g. night shift 22:00-07:00, OT 07:00-09:00 belongs to the next morning
        f += timedelta(days=1)
        t += timedelta(days=1)
    if t <= f:
        t += timedelta(days=1)
    return f, t


def classify(
    db: Session, emp: Employee, d: date, f: datetime, t: datetime
) -> tuple[str, dict[str, int], int]:
    """Split an OT interval into pay codes. Returns (day_kind, {code: minutes}, minutes inside shift)."""
    shift = shift_for(db, emp, d)
    rest = is_rest_day(db, emp, d)
    total = max(0, int((t - f).total_seconds() // 60))
    if shift is None:
        inside = 0
        work_minutes = 480
        brk = 60
    else:
        s0, s1 = shift_window(shift, d)
        inside = overlap_minutes(f, t, s0, s1)
        brk = shift.break_minutes
        work_minutes = int((s1 - s0).total_seconds() // 60) - brk
    outside = total - inside
    split: dict[str, int] = {}
    if not rest:
        if outside:
            split["OT15"] = outside
        return "workday", split, inside
    hol = min(inside, work_minutes)
    extra_inside = max(0, inside - work_minutes - brk)  # beyond the shift incl. its break
    code = "HOL20" if emp.pay_type == "Daily" else "HOL10"
    if hol:
        split[code] = hol
    if outside + extra_inside:
        split["OT30"] = outside + extra_inside
    return "holiday", split, 0


def minutes_in_week(db: Session, emp_id: str, d: date, exclude_id: Optional[int] = None) -> int:
    w0, w1 = week_bounds(d)
    q = select(OTRequest).where(
        OTRequest.employee_id == emp_id,
        OTRequest.work_date >= w0,
        OTRequest.work_date <= w1,
        OTRequest.status.in_(ACTIVE_OT),
    )
    if exclude_id:
        q = q.where(OTRequest.id != exclude_id)
    return sum(r.requested_minutes for r in db.scalars(q))


def touches_night(f: datetime, t: datetime) -> bool:
    cur = f
    while cur < t:
        tt = cur.time()
        if tt >= NIGHT_START or tt < NIGHT_END:
            return True
        cur += timedelta(minutes=15)
    return False


def check_ot(
    db: Session,
    emp: Optional[Employee],
    d: date,
    f: datetime,
    t: datetime,
    exclude_id: Optional[int] = None,
    today: Optional[date] = None,
    override: bool = False,
) -> tuple[list[Finding], str, dict[str, int]]:
    """All checks that run before an OT request is saved (blueprint section 6)."""
    today = today or date.today()
    out: list[Finding] = []
    if emp is None:
        return [Finding("emp_unknown", "block", {})], "workday", {}
    if t <= f:
        out.append(Finding("time_order", "block", {}))
        return out, "workday", {}
    if d < emp.join_date:
        out.append(Finding("before_join", "block", {"join": emp.join_date.isoformat()}))
    if emp.exit_date and d > emp.exit_date:
        out.append(Finding("after_exit", "block", {"exit": emp.exit_date.isoformat()}))
    elif emp.status != "Active" and not emp.exit_date:
        out.append(Finding("inactive", "flag", {}))

    day_kind, split, inside = classify(db, emp, d, f, t)
    minutes = sum(split.values())

    juvenile_age = get_setting(db, "juvenile_age") or 18
    age = emp.age_on(d)
    if age is not None and age < juvenile_age:
        out.append(Finding("juvenile_ot", "block", {"age": age}))
        if touches_night(f, t):
            out.append(Finding("juvenile_night", "block", {"age": age}))
    if age is None:
        out.append(Finding("no_birth_date", "flag", {}))

    if inside:
        out.append(Finding("inside_shift", "flag", {"minutes": inside}))

    limit_h = get_setting(db, "weekly_ot_limit_hours") or 36
    week_total = minutes_in_week(db, emp.employee_id, d, exclude_id) + minutes
    if week_total > limit_h * 60:
        out.append(
            Finding(
                "weekly_limit",
                "flag" if override else "block",
                {"hours": round(week_total / 60, 1), "limit": limit_h, "override": override},
            )
        )

    daily = get_setting(db, "daily_ot_limit_hours")
    if daily and minutes > float(daily) * 60:
        out.append(Finding("daily_limit", "flag", {"hours": round(minutes / 60, 1), "limit": daily}))
    if minutes > 12 * 60:
        out.append(Finding("very_long", "flag", {"hours": round(minutes / 60, 1)}))

    q = select(OTRequest).where(
        OTRequest.employee_id == emp.employee_id,
        OTRequest.status.in_(ACTIVE_OT),
        and_(OTRequest.ot_from < t, OTRequest.ot_to > f),
    )
    if exclude_id:
        q = q.where(OTRequest.id != exclude_id)
    dup = db.scalars(q).first()
    if dup:
        out.append(Finding("overlap", "block", {"ref": dup.ref}))

    if d < today:
        out.append(Finding("after_the_fact", "info", {}))
    if d > today + timedelta(days=31):
        out.append(Finding("far_future", "flag", {}))
    if minutes == 0 and not any(x.level == "block" for x in out):
        out.append(Finding("no_payable_minutes", "block", {}))
    return out, day_kind, split


def scan_cover(
    db: Session, req: OTRequest
) -> tuple[Optional[int], list[Finding], dict[str, int]]:
    """Compare an OT request with face scans. Returns (covered minutes, findings, payable split)."""
    att = db.scalar(
        select(DailyAttendance).where(
            DailyAttendance.employee_id == req.employee_id, DailyAttendance.work_date == req.work_date
        )
    )
    if att is None or not att.first_in or not att.last_out or att.scan_count < 2:
        return None, [Finding("no_scan", "flag", {})], {}
    covered = overlap_minutes(req.ot_from, req.ot_to, att.first_in, att.last_out)
    tol = int(get_setting(db, "scan_tolerance_min") or 0)
    findings: list[Finding] = []
    if covered + tol < req.requested_minutes:
        findings.append(
            Finding(
                "scan_short",
                "flag",
                {
                    "covered": round(covered / 60, 2),
                    "requested": req.requested_hours,
                    "first_in": att.first_in.strftime("%H:%M"),
                    "last_out": att.last_out.strftime("%H:%M"),
                },
            )
        )
        lo, hi = max(req.ot_from, att.first_in), min(req.ot_to, att.last_out)
        if hi > lo:
            _, pay, _ = classify(db, req.employee, req.work_date, lo, hi)
        else:
            pay = {}
    else:
        pay = dict((req.split or {}).get("req", {}))
    return covered, findings, pay


def payroll_period(cycle: str, d: date, staff_cutoff: int = 20) -> tuple[date, date]:
    """Pay period that contains work date d.
    STAFF_MONTHLY: (cutoff+1) of previous month to cutoff of this month (e.g. 21 Sep - 20 Oct).
    OPERATOR_HALF: 1-15 and 16-end of month."""
    if cycle == "OPERATOR_HALF":
        if d.day <= 15:
            return d.replace(day=1), d.replace(day=15)
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return d.replace(day=16), nxt - timedelta(days=1)
    if d.day > staff_cutoff:
        start = d.replace(day=staff_cutoff + 1)
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = nxt.replace(day=staff_cutoff)
    else:
        end = d.replace(day=staff_cutoff)
        prev_last = d.replace(day=1) - timedelta(days=1)
        start = prev_last.replace(day=staff_cutoff + 1)
    return start, end


def cycle_for(emp: Employee) -> str:
    return "OPERATOR_HALF" if emp.employee_group == "Operator" else "STAFF_MONTHLY"
