"""SAMPLE data for the demo. PDPA: no real person appears here. IDs YTR801-816
and YTOP951-990 only, names end with (SAMPLE). Never load this into a database
that holds real data."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ApprovalRequest,
    Employee,
    EmployeeRestricted,
    EmploymentEvent,
    Job,
    OrgUnit,
    OTRequest,
    OTBudget,
    Position,
    User,
    UserScope,
)
from ..security import hash_password
from ..services import approvals, facescan, health, ot, paybatch
from ..services.common import audit
from ..services.timecalc import is_rest_day, payroll_period, shift_for, shift_window

DEMO_PASSWORD = "Demo-2026-hr"

# id, name, group, unit, job, grade, heads (unit codes), user role(s), username
STAFF = [
    ("YTR801", "Pim Sample (SAMPLE)", "Manager", "HR", "MGR-DEPT", "MG", ["HR", "PI"], ["hr_manager"], "hr.manager"),
    ("YTR802", "Nok Sample (SAMPLE)", "Staff", "HR", "HR-OFF", "Staff 2", [], ["hr"], "hr.officer"),
    ("YTR803", "Fon Sample (SAMPLE)", "Staff", "HR", "HR-OFF", "Staff 1", [], ["hr"], "hr.officer2"),
    ("YTR804", "Ken Sample (SAMPLE)", "Japanese Management", "MGT-D", "GM", "GM", ["MGT-D", "SAF", "ENV", "IT"], ["gm"], "gm"),
    ("YTR805", "Chai Sample (SAMPLE)", "Manager", "PRD", "MGR-DEPT", "MG", ["PRD"], ["dept_head"], "prod.head"),
    ("YTR806", "Dao Sample (SAMPLE)", "Staff", "PRD-DRY", "PRD-SV", "SV", [], ["supervisor"], "sup.dry"),
    ("YTR807", "Lek Sample (SAMPLE)", "Staff", "PRD-PCL", "PRD-SV", "SV", [], ["supervisor"], "sup.pcl"),
    ("YTR808", "Mai Sample (SAMPLE)", "Manager", "QA", "MGR-DEPT", "MG", ["QA", "LAB", "SGP", "RMP"], ["dept_head"], "qa.head"),
    ("YTR809", "Ton Sample (SAMPLE)", "Manager", "MTN", "MGR-DEPT", "MG", ["MTN"], ["dept_head"], "mtn.head"),
    ("YTR810", "Ploy Sample (SAMPLE)", "Manager", "AF", "MGR-DEPT", "MG", ["AF"], ["dept_head"], None),
    ("YTR811", "Arm Sample (SAMPLE)", "Staff", "GA", "MGR-DEPT", "Ass MG", ["GA", "CSR"], ["dept_head"], None),
    ("YTR812", "Bee Sample (SAMPLE)", "Manager", "RMR", "MGR-DEPT", "MG", ["RMR"], ["dept_head"], "rmr.head"),
    ("YTR813", "Gift Sample (SAMPLE)", "Staff", "QA", "QA-INSP", "Staff 1", [], [], None),
    ("YTR814", "Jan Sample (SAMPLE)", "Staff", "LAB", "LAB-TECH", "Technician", [], [], None),
    ("YTR815", "Oat Sample (SAMPLE)", "Staff", "MTN", "MTN-TECH", "Technician 2", [], [], None),
    ("YTR816", "May Sample (SAMPLE)", "Staff", "SAF", "SAF-OFF", "Staff 2", [], [], None),
]

OP_UNITS = [("PRD-DRY", "OPR-DRY", 12), ("PRD-PCL", "OPR-PCL", 10), ("PRD-MIX", "OPR-MIX", 6), ("PRD-WH", "OPR-WH", 5), ("PRD-TRL", "OPR-TRL", 3), ("RMR", "OPR-RRC", 4)]
MY_FIRST = ["Aung", "Mya", "Kyaw", "Thida", "Zaw", "Nilar", "Htet", "Su", "Min", "Ei"]
TH_FIRST = ["Somchai", "Somsak", "Malee", "Wichai", "Suda", "Anan", "Kanya", "Prasit", "Ratana", "Boon"]


def _pos(db: Session, n: int, title: str, unit: str, job: str, grade: str, seats: int = 1, critical: bool = False) -> str:
    pid = f"POS-{n:04d}"
    if not db.get(Position, pid):
        db.add(Position(position_id=pid, title=title, org_unit_code=unit, job_code=job, grade_code=grade, seats=seats, budget_year=2026, critical=critical, min_backups_uo=2 if critical else 0))
    return pid


def seed_demo(db: Session, today: date | None = None) -> dict:
    rnd = random.Random(2026)
    today = today or date.today()
    if db.get(Employee, "YTR801"):
        return {"skipped": True}
    admin = User(username="it.admin", display_name="IT Admin (SAMPLE)", roles=["admin"], password_hash=hash_password(DEMO_PASSWORD))
    db.add(admin)
    n = 1
    for eid, name, group, unit, job, grade, heads, roles, username in STAFF:
        pid = _pos(db, n, f"{db.get(Job, job).title_en}, {db.get(OrgUnit, unit).name_en}", unit, job, grade)
        n += 1
        db.flush()
        emp = Employee(
            employee_id=eid, employee_group=group, pay_type="Monthly", title="Mr." if rnd.random() < 0.5 else "Ms.", full_name_en=name,
            full_name_th=name.replace("Sample", "ตัวอย่าง"), nationality="Japanese" if group == "Japanese Management" else "Thai",
            birth_date=date(1975 + rnd.randint(0, 20), rnd.randint(1, 12), rnd.randint(1, 28)), join_date=date(2010 + rnd.randint(0, 14), rnd.randint(1, 12), 1),
            org_unit_code=unit, position_id=pid, job_code=job, grade_code=grade, work_email=f"{eid.lower()}@sample.invalid", is_sample=True,
        )
        emp.gender = "M" if emp.title == "Mr." else "F"
        emp.position_since = emp.join_date
        db.add(emp)
        db.add(EmploymentEvent(employee_id=eid, event_type="hire", effective_date=emp.join_date, to_org_unit=unit, to_position=pid, to_job=job, to_grade=grade, to_group=group, applied=True, created_by="demo seed"))
        db.flush()
        for code in heads:
            db.get(OrgUnit, code).head_employee_id = eid
        if username:
            u = User(username=username, display_name=name, employee_id=eid, roles=roles + (["employee"] if "employee" not in roles else []), password_hash=hash_password(DEMO_PASSWORD))
            db.add(u)
            db.flush()
            if username == "sup.dry":
                db.add(UserScope(user_id=u.id, org_unit_code="PRD-DRY"))
            if username == "sup.pcl":
                db.add(UserScope(user_id=u.id, org_unit_code="PRD-PCL"))
    # Operators
    num = 951
    for unit, job, count in OP_UNITS:
        pid = _pos(db, n, f"Operator {db.get(OrgUnit, unit).name_en}", unit, job, "Operator", seats=count + 1, critical=(unit == "PRD-DRY"))
        n += 1
        db.flush()
        for k in range(count):
            eid = f"YTOP{num}"
            myanmar = rnd.random() < 0.55
            first = rnd.choice(MY_FIRST if myanmar else TH_FIRST)
            join = today - timedelta(days=rnd.randint(40, 2400))
            age = rnd.randint(19, 52)
            birth = date(today.year - age, rnd.randint(1, 12), rnd.randint(1, 28))
            if num == 957:  # juvenile worker: real step 2 finding (14 active operators under 18)
                birth = date(today.year - 17, 3, 9)
                join = today - timedelta(days=120)
            emp = Employee(
                employee_id=eid, employee_group="Operator", pay_type="Daily" if rnd.random() < 0.3 else "Monthly",
                title="Mr." if k % 2 == 0 else "Ms.", gender="M" if k % 2 == 0 else "F",
                full_name_en=f"{first} {chr(65 + k)}. (SAMPLE)", nationality="Myanmar" if myanmar else "Thai",
                full_name_my=f"{first} (SAMPLE)" if myanmar else None,
                birth_date=birth, join_date=join, org_unit_code=unit, position_id=pid, job_code=job, grade_code="Operator",
                team="A" if k % 2 == 0 else "B", is_sample=True,
                probation_end_date=join + timedelta(days=119) if (today - join).days < 130 else None,
            )
            emp.position_since = join
            db.add(emp)
            db.add(EmploymentEvent(employee_id=eid, event_type="hire", effective_date=join, to_org_unit=unit, to_position=pid, to_job=job, to_grade="Operator", to_group="Operator", applied=True, created_by="demo seed"))
            if myanmar and rnd.random() < 0.6:
                exp = today + timedelta(days=rnd.choice([25, 45, 200, 300, 400]))
                db.add(EmployeeRestricted(employee_id=eid, work_permit_no=f"WP-SAMPLE-{num}", work_permit_expiry=exp, passport_no=f"MX{num}000"))
            num += 1
    db.flush()
    # A few past exits, for the turnover chart
    reasons = ["RES-PAY", "RES-WORK", "RES-FAMILY", "CONTRACT", "RES-BOSS", "RES-CAREER", "PROBATION"]
    for i, eid in enumerate(["YTOP985", "YTOP986", "YTOP987", "YTOP988", "YTOP989", "YTOP990"]):
        e = db.get(Employee, eid)
        if e is None:
            continue
        ex = today - timedelta(days=20 + i * 47)
        if ex <= e.join_date:
            e.join_date = ex - timedelta(days=200)
        e.status, e.exit_date, e.exit_reason_code = "Resigned", ex, reasons[i % len(reasons)]
        db.add(EmploymentEvent(employee_id=eid, event_type="exit", effective_date=ex, from_org_unit=e.org_unit_code, reason_code=e.exit_reason_code, applied=True, created_by="demo seed"))
    # OT budget (sample numbers)
    for k in range(6):
        m = (today.replace(day=1) - timedelta(days=30 * k)).replace(day=1)
        if not db.scalar(select(OTBudget).where(OTBudget.month == m)):
            db.add(OTBudget(month=m, hours=900, note="SAMPLE budget"))
    db.flush()

    users = {u.username: u for u in db.scalars(select(User))}
    _demo_time(db, rnd, today, users)
    _demo_leave_exit(db, rnd, today, users)
    health.run(db, today)
    audit(db, admin, "seed", "demo", None, "SAMPLE demo data loaded")
    db.flush()
    return {"employees": db.query(Employee).count()}


def _demo_time(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Six weeks of face scans and OT, run through the real import, checks and approvals."""
    start = today - timedelta(days=42)
    end = today - timedelta(days=1)
    emps = [e for e in db.scalars(select(Employee).where(Employee.is_sample.is_(True)))]
    plan: dict[tuple[str, date], tuple[time, time]] = {}
    lines = []
    d = start
    while d <= end:
        for e in emps:
            if not e.is_active_on(d):
                continue
            sh = shift_for(db, e, d)
            if sh is None:
                continue
            rest = is_rest_day(db, e, d)
            holiday_work = rest and e.employee_group == "Operator" and e.employee_id != "YTOP957" and rnd.random() < 0.08
            if (rest and not holiday_work) or rnd.random() < 0.04:
                continue  # rest day or absent
            s0, s1 = shift_window(sh, d)
            if holiday_work:
                extra = rnd.choice([0, 0, 60, 120])
                t_in, t_out = s0 - timedelta(minutes=rnd.randint(5, 30)), s1 + timedelta(minutes=extra + rnd.randint(2, 10))
                plan[(e.employee_id, d)] = (s0.time(), (s1 + timedelta(minutes=extra)).time())
                for t in (t_in, t_out):
                    lines.append(f"{facescan.employee_to_scan_user(e.employee_id)}  {t:%d-%m-%Y}  {t:%H:%M}  1  I")
                continue
            t_in = s0 - timedelta(minutes=rnd.randint(5, 40)) + (timedelta(minutes=rnd.randint(6, 25)) if rnd.random() < 0.05 else timedelta())
            stay = 0
            if e.employee_group == "Operator" and rnd.random() < 0.35 and e.employee_id != "YTOP957":
                stay = rnd.choice([60, 120, 120, 180, 240])
            elif e.employee_group != "Operator" and rnd.random() < 0.2:
                stay = rnd.choice([60, 90, 120])
            t_out = s1 + timedelta(minutes=stay + rnd.randint(2, 15))
            if stay and rnd.random() < 0.08:
                t_out = s1 + timedelta(minutes=max(0, stay - 60))  # left early: scan will not cover the OT
            if stay:
                plan[(e.employee_id, d)] = ((s1).time(), (s1 + timedelta(minutes=stay)).time())
            for t in (t_in, t_out):
                lines.append(f"{facescan.employee_to_scan_user(e.employee_id)}  {t:%d-%m-%Y}  {t:%H:%M}  1  I")
        d += timedelta(days=1)
    lines.append(f"0000003029  {end:%d-%m-%Y}  07:55  1  I")  # unknown scan user, as in the real file
    sup = {"PRD-DRY": users["sup.dry"], "PRD-PCL": users["sup.pcl"]}
    hr1, hr2, head = users["hr.officer"], users["hr.officer2"], users["prod.head"]
    other = users["hr.manager"]
    # OT requests, entered the day before (or for some, after the fact)
    for (eid, d), (t0, t1) in sorted(plan.items(), key=lambda x: x[0][1]):
        e = db.get(Employee, eid)
        submitter = sup.get(e.org_unit_code) or other
        if rnd.random() < 0.1:
            continue  # stayed but nobody asked for OT: flagged as OT_NO_REQUEST
        res = ot.create(db, submitter, e, d, t0, t1, rnd.choice(["Shipment packing", "Line cleaning", "Backlog", "Machine breakdown", "Stock count"]))
        if not res.ok:
            continue
        if rnd.random() < 0.88:  # most were entered the day before, as the process asks
            entered = datetime.combine(d - timedelta(days=1), time(15, rnd.randint(0, 59)))
            res.request.pre_approved = True
            res.request.created_at = entered
            res.request.flags = [f for f in (res.request.flags or []) if f.get("code") != "after_the_fact"]
            appr = db.get(ApprovalRequest, res.request.approval_id)
            appr.created_at = entered
    db.flush()
    facescan.import_scans(db, f"timelog_SAMPLE_{start:%Y%m%d}_{end:%Y%m%d}.txt", "\n".join(lines), hr1)
    # Approvals: the Department Head approves most; HR verifies older ones
    for appr in db.scalars(select(ApprovalRequest).where(ApprovalRequest.request_type == "OT", ApprovalRequest.status == "pending")).all():
        req = db.get(OTRequest, int(appr.entity_id))
        step = appr.active_step
        if step is None or step.name != "dept_head":
            continue
        approver = next((u for u in users.values() if u.employee_id and u.employee_id == step.approver_employee_id), None)
        if approver is None or req.work_date > today - timedelta(days=2):
            continue
        try:
            approvals.decide(db, appr, approver, "approved", "OK")
        except approvals.ApprovalError:
            continue
        if req.work_date < today - timedelta(days=9) and req.scan_minutes is not None and req.scan_minutes + 10 >= req.requested_minutes:
            verifier = hr2 if appr.submitted_by_user_id == hr1.id else hr1
            try:
                approvals.decide(db, appr, verifier, "approved", "Verified against face scan")
            except approvals.ApprovalError:
                pass
    db.flush()
    # One sent batch for the last full operator half-month, so the variance check has a base
    p0, p1 = payroll_period("OPERATOR_HALF", today - timedelta(days=16))
    prev0, prev1 = payroll_period("OPERATOR_HALF", p0 - timedelta(days=1))
    try:
        b0 = paybatch.build(db, hr1, "OPERATOR_HALF", prev0, prev1)
        paybatch.mark(db, hr2, b0, "checked")
        paybatch.mark(db, hr2, b0, "sent")
        paybatch.mark(db, hr1, b0, "imported", "SAMPLE: imported, 0 errors")
        paybatch.build(db, hr1, "OPERATOR_HALF", p0, p1)
    except paybatch.BatchError:
        pass
    db.flush()


def _approve_head(db: Session, appr: ApprovalRequest, users: dict) -> None:
    step = appr.active_step
    if step is None:
        return
    approver = next((u for u in users.values() if u.employee_id and u.employee_id == step.approver_employee_id), None)
    if approver is not None:
        try:
            approvals.decide(db, appr, approver, "approved", "OK")
        except approvals.ApprovalError:
            pass


def _demo_leave_exit(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Leave for most of the absent days, a few planned vacations, and two exit cases."""
    from ..models import DailyAttendance, LeaveRequest
    from ..services import exits, leave

    hr = users["hr.officer"]
    sup = {"PRD-DRY": users["sup.dry"], "PRD-PCL": users["sup.pcl"]}
    absent = db.scalars(select(DailyAttendance).where(DailyAttendance.day_code == "a").order_by(DailyAttendance.work_date)).all()
    first_day = min((a.work_date for a in absent), default=None)
    for a in absent:
        if a.work_date == first_day or rnd.random() < 0.3:  # first day of the scan file is incomplete
            continue  # stays a no-show
        e = a.employee
        code = rnd.choice(["SICK", "SICK", "SICK", "PERS", "VAC"])
        res = leave.create(db, sup.get(e.org_unit_code) or hr, e, code, a.work_date, a.work_date, reason=rnd.choice(["Fever", "Family matter", "Hospital visit", "Child sick", ""]))
        if res.ok and rnd.random() < 0.9:
            _approve_head(db, db.get(ApprovalRequest, res.request.approval_id), users)
    # three-day sick leave with no certificate yet: goes to HR check
    e = db.get(Employee, "YTOP958")
    d0 = today - timedelta(days=12)
    while is_rest_day(db, e, d0):
        d0 -= timedelta(days=1)
    leave.create(db, hr, e, "SICK", d0, d0 + timedelta(days=3), reason="Dengue fever (SAMPLE)")
    # planned vacations, waiting for the Department Head
    for eid, days_ahead in (("YTOP961", 9), ("YTOP970", 16), ("YTR805", 5)):
        e = db.get(Employee, eid)
        if e is None:
            continue
        d = today + timedelta(days=days_ahead)
        res = leave.create(db, users["hr.manager"], e, "VAC", d, d + timedelta(days=1), reason="Going home (SAMPLE)")
        if res.ok and eid == "YTR805":
            _approve_head(db, db.get(ApprovalRequest, res.request.approval_id), users)
    db.flush()
    # Exit cases for the two most recent exits
    head, it = users["prod.head"], users["it.admin"]
    samples = [
        ("YTOP985", {"job": 4, "boss": 2, "team": 4, "pay": 3, "shift": 2, "growth": 3, "safety": 4}, "maybe",
         "หัวหน้าพูดจาแรง และต้องทำกะดึกติดกันหลายเดือน", "Night shift every month is too tiring. Supervisor shouts when the line stops."),
        ("YTOP986", {"job": 3, "boss": 4, "team": 4, "pay": 2, "shift": 3, "growth": 2, "safety": 4}, "yes",
         "ကျွန်တော့်အမေ နေမကောင်းလို့ ရွာပြန်ရမယ်", "Mother is ill in Myanmar, going home. Would come back later."),
    ]
    for eid, scores, back, reason, comments in samples:
        e = db.get(Employee, eid)
        if e is None or not e.exit_date:
            continue
        case = exits.open_case(db, e, e.exit_date, e.exit_reason_code, hr)
        for item in case.items:
            if item["code"] in ("HANDOVER", "ASSETS"):
                exits.tick(db, case, head, item["code"], True)
            elif item["code"] == "IT_ACCESS":
                exits.tick(db, case, it, item["code"], True)
            elif eid == "YTOP986" or item["code"] in ("ID_CARD", "FINAL_PAY"):
                exits.tick(db, case, hr, item["code"], True)
        exits.save_interview(db, case, hr, scores, back, reason, comments)
        exits.suggest_reason(db, case)
        if eid == "YTOP986":
            exits.confirm_reason(db, case, users["hr.manager"], case.ai_reason_code or "RES-FAMILY")
            exits.close(db, case, users["hr.manager"])
    db.flush()
