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
    # Unplanned absence: no-show plus leave types that count as absence (sick, personal, unpaid).
    from ..models import LeaveType

    absence_codes = {"a"} | set(db.scalars(select(LeaveType.day_code).where(LeaveType.counts_as_absence.is_(True))).all())
    absent = [a for a in workdays if a.day_code in absence_codes]
    by_code: dict[str, int] = defaultdict(int)
    for a in workdays:
        if a.day_code and a.day_code not in ("1", "h"):
            by_code[a.day_code] += 1
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
        "no_show": by_code.get("a", 0),
        "by_code": dict(by_code),
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


# ---------------- Phase 6: retention, absence patterns, people cost, workforce mix ----------------

VOLUNTARY = ("RES-PAY", "RES-WORK", "RES-BOSS", "RES-CAREER", "RES-FAMILY", "RES-HEALTH", "RES-OTHER")
NOT_TURNOVER = ("RETIRED", "DECEASED", "TO-STAFF", "TO-GROUP")  # left the ID, not the company, or not a choice
TENURE_BANDS = [(0, 90, "Under 3 months"), (90, 365, "3-12 months"), (365, 730, "1-2 years"), (730, 1825, "2-5 years"), (1825, 10 ** 6, "5 years +")]
AGE_BANDS = [(0, 18, "Under 18"), (18, 25, "18-24"), (25, 35, "25-34"), (35, 45, "35-44"), (45, 55, "45-54"), (55, 200, "55 +")]


def band(v: int, bands) -> str:
    return next(lbl for lo, hi, lbl in bands if lo <= v < hi)


def retention(db: Session, scope: Scope, today: Optional[date] = None) -> dict:
    today = today or date.today()
    y0 = today - timedelta(days=365)
    emps = employees(db, scope)
    leavers = [e for e in emps if e.exit_date and y0 < e.exit_date <= today and (e.exit_reason_code or "") not in NOT_TURNOVER]
    start = sum(1 for e in emps if e.is_active_on(y0))
    end = sum(1 for e in emps if e.is_active_on(today))
    avg = (start + end) / 2 or 1
    vol = [e for e in leavers if (e.exit_reason_code or "") in VOLUNTARY]
    tenure = defaultdict(int)
    for e in leavers:
        tenure[band((e.exit_date - e.join_date).days, TENURE_BANDS)] += 1
    first_year = sum(1 for e in leavers if (e.exit_date - e.join_date).days < 365)
    # people employed a year ago who are still here
    stayed = sum(1 for e in emps if e.is_active_on(y0) and e.is_active_on(today))
    # hires 12-24 months ago: still here after one year?
    cohort = [e for e in emps if today - timedelta(days=730) < e.join_date <= y0]
    kept = sum(1 for e in cohort if not e.exit_date or (e.exit_date - e.join_date).days >= 365)
    def split(key):
        out: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])  # headcount now, leavers, average headcount
        for e in emps:
            k = key(e)
            if e.is_active_on(today):
                out[k][0] += 1
                out[k][2] += 0.5
            if e.is_active_on(y0):
                out[k][2] += 0.5
        for e in leavers:
            out[key(e)][1] += 1
        return out

    by_dept, by_group = split(dept_of), split(lambda e: e.employee_group)
    return {
        "leavers": len(leavers), "voluntary": len(vol), "involuntary": len(leavers) - len(vol),
        "turnover": round(100 * len(leavers) / avg, 1), "vol_turnover": round(100 * len(vol) / avg, 1),
        "stability": round(100 * stayed / start, 1) if start else None,
        "first_year": first_year, "first_year_pct": round(100 * first_year / len(leavers)) if leavers else None,
        "tenure": [(lbl, tenure.get(lbl, 0)) for _, _, lbl in TENURE_BANDS],
        "cohort": len(cohort), "cohort_kept": kept, "cohort_pct": round(100 * kept / len(cohort)) if cohort else None,
        "by_dept": sorted(((d, hc, n, round(100 * n / av, 1) if av else None) for d, (hc, n, av) in by_dept.items()), key=lambda x: -(x[3] or 0)),
        "by_group": sorted(((g, hc, n, round(100 * n / av, 1) if av else None) for g, (hc, n, av) in by_group.items()), key=lambda x: x[0]),
    }


def absence_patterns(db: Session, scope: Scope, d0: date, d1: date) -> dict:
    """Bradford factor (spells squared x days) finds short, repeated absences; and how many
    absent days touch a rest day or holiday (long weekends)."""
    from ..models import LeaveType

    codes = {"a"} | set(db.scalars(select(LeaveType.day_code).where(LeaveType.counts_as_absence.is_(True))).all())
    rows = db.scalars(select(DailyAttendance).where(DailyAttendance.work_date >= d0 - timedelta(days=1), DailyAttendance.work_date <= d1 + timedelta(days=1))
                      .order_by(DailyAttendance.employee_id, DailyAttendance.work_date)).all()
    if not scope.all:
        rows = [a for a in rows if a.employee.org_unit_code in scope.org_units]
    by_emp: dict[str, list] = defaultdict(list)
    for a in rows:
        by_emp[a.employee_id].append(a)
    out, near_rest, total = [], 0, 0
    for eid, days in by_emp.items():
        rest = {a.work_date for a in days if a.is_rest_day}
        absent = sorted(a.work_date for a in days if not a.is_rest_day and a.day_code in codes and d0 <= a.work_date <= d1)
        if not absent:
            continue
        spells, prev = 0, None
        for d in absent:
            # a new spell unless yesterday (or the last workday before a rest day) was absent too
            gap = prev is not None and all((prev + timedelta(days=k)) in rest for k in range(1, (d - prev).days))
            if prev is None or not ((d - prev).days == 1 or gap):
                spells += 1
            prev = d
        for d in absent:
            total += 1
            if (d - timedelta(days=1)) in rest or (d + timedelta(days=1)) in rest:
                near_rest += 1
        out.append((days[0].employee, spells, len(absent), spells * spells * len(absent)))
    out.sort(key=lambda x: -x[3])
    return {"bradford": out[:15], "near_rest": near_rest, "absent": total, "near_rest_pct": round(100 * near_rest / total) if total else None}


def people_cost(db: Session, scope: Scope, months: int = 12, today: Optional[date] = None) -> dict:
    """From the totals Bplus sends back per Org Unit and month (no individual pay in the app)."""
    from ..models import PayTotal

    today = today or date.today()
    m_end = month_start(today)
    keys = [add_months(m_end, -k) for k in range(months, 0, -1)]
    rows = [r for r in db.scalars(select(PayTotal).where(PayTotal.month >= keys[0], PayTotal.month <= keys[-1]))
            if scope.all or r.org_unit_code in scope.org_units]
    units = {u.code: u for u in db.scalars(select(OrgUnit))}

    def dept(code):
        u = units.get(code)
        d = u.department if u else None
        return (d or u).name_en if (d or u) else code

    tot = {k: [0.0, 0.0, 0] for k in keys}
    by_dept: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    last = keys[-1]
    for r in rows:
        t = tot[r.month]
        t[0] += float(r.total_pay_cost or 0)
        t[1] += float(r.ot_cost or 0)
        t[2] += r.headcount_paid or 0
        if r.month == last:
            b = by_dept[dept(r.org_unit_code)]
            b[0] += float(r.total_pay_cost or 0)
            b[1] += float(r.ot_cost or 0)
            b[2] += r.headcount_paid or 0
    labels = [k.strftime("%m/%y") for k in keys]
    cost = [round(tot[k][0]) for k in keys]
    ot = [round(tot[k][1]) for k in keys]
    per_head = [round(tot[k][0] / tot[k][2]) if tot[k][2] else 0 for k in keys]
    ot_share = [round(100 * tot[k][1] / tot[k][0], 1) if tot[k][0] else 0 for k in keys]
    depts = sorted(((d, round(c), round(o), h, round(c / h) if h else None, round(100 * o / c, 1) if c else None) for d, (c, o, h) in by_dept.items()),
                   key=lambda x: -x[1])
    return {"labels": labels, "cost": cost, "ot": ot, "base": [c - o for c, o in zip(cost, ot)],
            "base_k": [round((c - o) / 1000) for c, o in zip(cost, ot)], "ot_k": [round(o / 1000) for o in ot], "per_head": per_head, "ot_share": ot_share,
            "depts": depts, "last": last, "has": any(cost), "year_cost": sum(cost), "year_ot": sum(ot)}


def workforce_mix(db: Session, scope: Scope, today: Optional[date] = None) -> dict:
    today = today or date.today()
    emps = [e for e in employees(db, scope) if e.is_active_on(today)]
    groups = sorted({e.employee_group for e in emps})
    nats = sorted({e.nationality for e in emps})
    grid = {(g, n): 0 for g in groups for n in nats}
    age, ten, gender = defaultdict(int), defaultdict(int), defaultdict(int)
    for e in emps:
        grid[(e.employee_group, e.nationality)] += 1
        a = e.age_on(today)
        if a is not None:
            age[band(a, AGE_BANDS)] += 1
        ten[band((today - e.join_date).days, TENURE_BANDS)] += 1
        gender[e.gender or "?"] += 1
    ages = [e.age_on(today) for e in emps if e.age_on(today) is not None]
    return {"total": len(emps), "groups": groups, "nats": nats, "grid": grid,
            "age": [(lbl, age.get(lbl, 0)) for _, _, lbl in AGE_BANDS], "tenure": [(lbl, ten.get(lbl, 0)) for _, _, lbl in TENURE_BANDS],
            "gender": dict(gender), "avg_age": round(sum(ages) / len(ages), 1) if ages else None,
            "avg_tenure": round(sum((today - e.join_date).days for e in emps) / 365 / len(emps), 1) if emps else None}
