"""Face scan import (timelog_*.txt) and the daily attendance summary.

Export format seen in Oct 2026: one punch per line, whitespace or comma separated:
    0000002898  16-10-2026  06:58  1  I
Scan user 0000001xxx = YTRxxx, 0000002xxx = YTOPxxx (step 3 finding)."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DailyAttendance, Employee, OTRequest, ScanImport, ScanPunch, User
from .common import audit, get_setting
from .timecalc import is_rest_day, scan_cover, shift_for, shift_window

LINE_RE = re.compile(
    r"^\s*(?P<user>\d{6,12})[\s,;]+(?P<d>\d{1,2})[-/.](?P<m>\d{1,2})[-/.](?P<y>\d{4})[\s,;]+(?P<hh>\d{1,2}):(?P<mm>\d{2})"
)


def scan_user_to_employee(scan_user: str) -> Optional[str]:
    s = scan_user.zfill(10)
    if s.startswith("0000001"):
        return f"YTR{s[-3:]}"
    if s.startswith("0000002"):
        return f"YTOP{s[-3:]}"
    return None


def employee_to_scan_user(emp_id: str) -> Optional[str]:
    if emp_id.startswith("YTOP"):
        return f"0000002{emp_id[4:]}"
    if emp_id.startswith("YTR"):
        return f"0000001{emp_id[3:]}"
    return None


def parse_lines(lines: Iterable[str]) -> tuple[list[tuple[str, datetime]], list[str]]:
    punches, bad = [], []
    for raw in lines:
        if not raw.strip():
            continue
        m = LINE_RE.match(raw)
        if not m:
            bad.append(raw.strip()[:80])
            continue
        try:
            at = datetime(int(m["y"]), int(m["m"]), int(m["d"]), int(m["hh"]), int(m["mm"]))
        except ValueError:
            bad.append(raw.strip()[:80])
            continue
        punches.append((m["user"].zfill(10), at))
    return punches, bad


def import_scans(db: Session, filename: str, text: str, user: Optional[User]) -> ScanImport:
    punches, bad = parse_lines(text.splitlines())
    known = set(db.scalars(select(Employee.employee_id)).all())
    imp = ScanImport(filename=filename, imported_by=user.label if user else "system", lines_total=len(punches))
    db.add(imp)
    db.flush()
    unknown: set[str] = set()
    new = 0
    existing = set()
    if punches:
        lo = min(p[1] for p in punches)
        hi = max(p[1] for p in punches)
        existing = {
            (u, at)
            for u, at in db.execute(
                select(ScanPunch.scan_user, ScanPunch.punched_at).where(
                    ScanPunch.punched_at >= lo, ScanPunch.punched_at <= hi
                )
            )
        }
    touched: dict[str, set[date]] = defaultdict(set)
    for scan_user, at in punches:
        if (scan_user, at) in existing:
            continue
        existing.add((scan_user, at))
        emp_id = scan_user_to_employee(scan_user)
        if emp_id not in known:
            unknown.add(scan_user)
            emp_id = None
        db.add(ScanPunch(scan_user=scan_user, employee_id=emp_id, punched_at=at, import_id=imp.id))
        new += 1
        if emp_id:
            touched[emp_id].add(at.date())
    imp.lines_new = new
    imp.unknown_users = sorted(unknown) + ([f"unreadable: {b}" for b in bad[:20]] if bad else [])
    if punches:
        imp.date_from = min(p[1] for p in punches).date()
        imp.date_to = max(p[1] for p in punches).date()
    db.flush()
    if imp.date_from:
        rebuild_attendance(db, imp.date_from - timedelta(days=1), imp.date_to)
    audit(db, user, "import", "scan_import", imp.id, f"{filename}: {new} new punches, {len(unknown)} unknown IDs")
    return imp


def rebuild_attendance(db: Session, d0: date, d1: date, employee_ids: Optional[list[str]] = None) -> int:
    before = int(get_setting(db, "scan_window_before_min") or 120)
    after = int(get_setting(db, "scan_window_after_min") or 600)
    q = select(Employee)
    if employee_ids:
        q = q.where(Employee.employee_id.in_(employee_ids))
    emps = [e for e in db.scalars(q) if e.join_date <= d1 and (e.exit_date is None or e.exit_date >= d0)]
    punch_rows = db.execute(
        select(ScanPunch.employee_id, ScanPunch.punched_at).where(
            ScanPunch.punched_at >= datetime.combine(d0, datetime.min.time()) - timedelta(hours=3),
            ScanPunch.punched_at < datetime.combine(d1 + timedelta(days=2), datetime.min.time()),
            ScanPunch.employee_id.is_not(None),
        )
    ).all()
    by_emp: dict[str, list[datetime]] = defaultdict(list)
    for eid, at in punch_rows:
        by_emp[eid].append(at)
    from .leave import approved_leave_codes

    on_leave = approved_leave_codes(db, d0, d1)
    count = 0
    for emp in emps:
        times = sorted(by_emp.get(emp.employee_id, []))
        used: set[datetime] = set()
        d = d0
        while d <= d1:
            if not emp.is_active_on(d):
                d += timedelta(days=1)
                continue
            shift = shift_for(db, emp, d)
            rest = is_rest_day(db, emp, d)
            first = last = None
            n = 0
            late = scan_ot = 0
            if shift:
                s0, s1 = shift_window(shift, d)
                w0, w1 = s0 - timedelta(minutes=before), s1 + timedelta(minutes=after)
                inwin = [t for t in times if w0 <= t <= w1 and t not in used]
                used.update(inwin)
                if inwin:
                    first, last, n = inwin[0], inwin[-1], len(inwin)
                    late = max(0, int((first - s0).total_seconds() // 60)) if not rest else 0
                    scan_ot = max(0, int((last - s1).total_seconds() // 60)) if n >= 2 else 0
            if n:
                code = "1"
            elif rest:
                code = "h"
            else:
                code = on_leave.get((emp.employee_id, d), "a")
            att = db.scalar(
                select(DailyAttendance).where(
                    DailyAttendance.employee_id == emp.employee_id, DailyAttendance.work_date == d
                )
            )
            if att is None:
                att = DailyAttendance(employee_id=emp.employee_id, work_date=d)
                db.add(att)
            elif att.source in ("manual", "leave"):
                d += timedelta(days=1)
                continue
            att.shift_code = shift.code if shift else None
            att.is_rest_day = rest
            att.first_in, att.last_out, att.scan_count = first, last, n
            att.day_code = code
            att.late_minutes = late
            att.scan_ot_minutes = scan_ot
            att.source = "scan"
            count += 1
            d += timedelta(days=1)
    db.flush()
    refresh_ot_scan_checks(db, d0, d1)
    return count


def refresh_ot_scan_checks(db: Session, d0: date, d1: date) -> None:
    reqs = db.scalars(
        select(OTRequest).where(
            OTRequest.work_date >= d0,
            OTRequest.work_date <= d1,
            OTRequest.status.in_(("pending", "approved", "verified")),
        )
    ).all()
    for r in reqs:
        apply_scan_check(db, r)


def apply_scan_check(db: Session, r: OTRequest) -> None:
    covered, findings, pay = scan_cover(db, r)
    r.scan_minutes = covered
    flags = [f for f in (r.flags or []) if f["code"] not in ("no_scan", "scan_short")]
    flags += [f.as_dict() for f in findings]
    r.flags = flags
    split = dict(r.split or {})
    split["pay"] = pay
    r.split = split
    r.payable_minutes = sum(pay.values()) if covered is not None else None
