"""Numbers for the dashboards (M13). Everything is computed from the shared
tables, so there is no separate data warehouse to keep in sync."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DailyAttendance,
    DataIssue,
    Employee,
    FormCapture,
    OrgUnit,
    OTBudget,
    OTRequest,
)
from .common import Scope
from .timecalc import week_bounds

COUNTED = ("pending", "approved", "verified", "paid")


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def dept_of(emp: Employee) -> str:
    dep = emp.org_unit.department if emp.org_unit else None
    return dep.name_en if dep else (emp.org_unit.name_en if emp.org_unit else "?")


def employees(db: Session, scope: Scope) -> list[Employee]:
    rows = db.scalars(select(Employee)).all()
    if scope.all:
        return list(rows)
    return [e for e in rows if e.org_unit_code in scope.org_units or e.employee_id == scope.employee_id]


def headcount(db: Session, scope: Scope, on: Optional[date] = None) -> dict:
    on = on or date.today()
    emps = [e for e in employees(db, scope) if e.is_active_on(on)]
    by_group: dict[str, int] = defaultdict(int)
    by_dept: dict[str, int] = defaultdict(int)
    by_nat: dict[str, int] = defaultdict(int)
    for e in emps:
        by_group[e.employee_group] += 1
        by_dept[dept_of(e)] += 1
        by_nat[e.nationality] += 1
    return {
        "total": len(emps),
        "by_group": dict(sorted(by_group.items())),
        "by_dept": sorted(by_dept.items(), key=lambda x: -x[1]),
        "by_nationality": dict(by_nat),
    }


def movement(db: Session, scope: Scope, months: int = 12, today: Optional[date] = None) -> dict:
    """Joins, exits, headcount and turnover % per month."""
    today = today or date.today()
    emps = employees(db, scope)
    labels, joins, exits, hc, turnover = [], [], [], [], []
    for k in range(months - 1, -1, -1):
        m0 = add_months(month_start(today), -k)
        m1 = add_months(m0, 1) - timedelta(days=1)
        j = sum(1 for e in emps if m0 <= e.join_date <= m1)
        x = sum(1 for e in emps if e.exit_date and m0 <= e.exit_date <= m1)
        start_hc = sum(1 for e in emps if e.is_active_on(m0))
        end_hc = sum(1 for e in emps if e.is_active_on(m1))
        avg = (start_hc + end_hc) / 2 or 1
        labels.append(m0.strftime("%b %y"))
        joins.append(j)
        exits.append(x)
        hc.append(end_hc)
        turnover.append(round(x / avg * 100, 1))
    return {"labels": labels, "joins": joins, "exits": exits, "headcount": hc, "turnover": turnover}


def exit_reasons(db: Session, scope: Scope, since: date, until: Optional[date] = None) -> list[tuple[str, int]]:
    out: dict[str, int] = defaultdict(int)
    for e in employees(db, scope):
        if e.exit_date and e.exit_date >= since and (until is None or e.exit_date <= until):
            out[e.exit_reason_code or "NOT-RECORDED"] += 1
    return sorted(out.items(), key=lambda x: -x[1])


def ot_rows(db: Session, scope: Scope, d0: date, d1: date) -> list[OTRequest]:
    rows = db.scalars(
        select(OTRequest).where(OTRequest.work_date >= d0, OTRequest.work_date <= d1, OTRequest.status.in_(COUNTED))
    ).all()
    if scope.all:
        return list(rows)
    return [r for r in rows if r.employee.org_unit_code in scope.org_units or r.employee_id == scope.employee_id]


def ot_hours(r: OTRequest) -> float:
    m = r.payable_minutes if r.payable_minutes is not None and r.status in ("verified", "paid") else r.requested_minutes
    return (m or 0) / 60


def ot_summary(db: Session, scope: Scope, d0: date, d1: date) -> dict:
    rows = ot_rows(db, scope, d0, d1)
    by_dept: dict[str, float] = defaultdict(float)
    by_person: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    by_code: dict[str, float] = defaultdict(float)
    by_group: dict[str, float] = defaultdict(float)
    weekly: dict[tuple[str, date], float] = defaultdict(float)
    for r in rows:
        h = ot_hours(r)
        by_dept[dept_of(r.employee)] += h
        by_person[r.employee_id] += h
        by_group[r.employee.employee_group] += h
        names[r.employee_id] = r.employee.full_name_en
        for code, mins in ((r.split or {}).get("req") or {}).items():
            by_code[code] += mins / 60
        weekly[(r.employee_id, week_bounds(r.work_date)[0])] += r.requested_minutes / 60
    over = sorted([(eid, w, h) for (eid, w), h in weekly.items() if h > 36], key=lambda x: -x[2])
    scan_short = [r for r in rows if any(f["code"] == "scan_short" for f in (r.flags or []))]
    no_scan = [r for r in rows if r.status in ("approved", "verified") and r.scan_minutes is None]
    late_entry = [r for r in rows if not r.pre_approved]
    return {
        "total_hours": round(sum(by_dept.values()), 1),
        "requests": len(rows),
        "people": len(by_person),
        "by_dept": sorted(((k, round(v, 1)) for k, v in by_dept.items()), key=lambda x: -x[1]),
        "by_group": dict(by_group),
        "by_code": {k: round(v, 1) for k, v in by_code.items()},
        "top_people": [(f"{eid} {names[eid]}", round(h, 1), eid) for eid, h in sorted(by_person.items(), key=lambda x: -x[1])[:10]],
        "over_limit": over,
        "scan_short": scan_short,
        "no_scan": no_scan,
        "late_entry_pct": round(len(late_entry) / len(rows) * 100) if rows else 0,
        "pending": sum(1 for r in rows if r.status == "pending"),
    }


def ot_by_month(db: Session, scope: Scope, months: int = 6, today: Optional[date] = None) -> dict:
    today = today or date.today()
    labels, staff, ops, budget = [], [], [], []
    for k in range(months - 1, -1, -1):
        m0 = add_months(month_start(today), -k)
        m1 = add_months(m0, 1) - timedelta(days=1)
        rows = ot_rows(db, scope, m0, m1)
        labels.append(m0.strftime("%b"))
        staff.append(round(sum(ot_hours(r) for r in rows if r.employee.employee_group != "Operator"), 1))
        ops.append(round(sum(ot_hours(r) for r in rows if r.employee.employee_group == "Operator"), 1))
        b = db.scalar(select(func.sum(OTBudget.hours)).where(OTBudget.month == m0))
        budget.append(float(b) if b else None)
    return {"labels": labels, "staff": staff, "operators": ops, "budget": budget if any(budget) else None}


def ot_by_week(db: Session, scope: Scope, weeks: int = 8, today: Optional[date] = None) -> dict:
    today = today or date.today()
    w_end = week_bounds(today)[0]
    labels, vals = [], []
    for k in range(weeks - 1, -1, -1):
        w0 = w_end - timedelta(days=7 * k)
        rows = ot_rows(db, scope, w0, w0 + timedelta(days=6))
        labels.append(w0.strftime("%d/%m"))
        vals.append(round(sum(ot_hours(r) for r in rows), 1))
    return {"labels": labels, "values": vals}


def absence(db: Session, scope: Scope, d0: date, d1: date) -> dict:
    rows = db.scalars(select(DailyAttendance).where(DailyAttendance.work_date >= d0, DailyAttendance.work_date <= d1)).all()
    if not scope.all:
        rows = [a for a in rows if a.employee.org_unit_code in scope.org_units]
    workdays = [a for a in rows if not a.is_rest_day]
    absent = [a for a in workdays if a.day_code == "a"]
    late = [a for a in workdays if a.late_minutes > 5]
    by_dow: dict[int, int] = defaultdict(int)
    for a in absent:
        by_dow[a.work_date.weekday()] += 1
    by_person: dict[str, int] = defaultdict(int)
    for a in absent:
        by_person[a.employee_id] += 1
    return {
        "workdays": len(workdays),
        "absent": len(absent),
        "rate": round(len(absent) / len(workdays) * 100, 1) if workdays else 0,
        "late": len(late),
        "by_dow": [by_dow.get(i, 0) for i in range(7)],
        "frequent": sorted(by_person.items(), key=lambda x: -x[1])[:10],
    }


def open_issue_count(db: Session, scope: Scope) -> int:
    q = select(DataIssue).where(DataIssue.status == "open")
    rows = db.scalars(q).all()
    if scope.all:
        return len(rows)
    return sum(1 for i in rows if i.org_unit_code in scope.org_units)


def review_queue_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(FormCapture).where(FormCapture.status.in_(("needs_review", "extracted")))) or 0


def units_tree(db: Session) -> list[OrgUnit]:
    return list(db.scalars(select(OrgUnit).where(OrgUnit.parent_code.is_(None)).order_by(OrgUnit.sort_order)))
