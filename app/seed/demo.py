"""SAMPLE data for the demo. PDPA: no real person appears here. IDs YTR801-816
and YTOP951-990 only, names end with (SAMPLE). Never load this into a database
that holds real data."""
from __future__ import annotations

import random
import uuid
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
            if num == 983:  # probation ends in about two weeks: review to fill in
                join = today - timedelta(days=104)
            if num == 984:  # started this month: onboarding still open
                join = today - timedelta(days=12)
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
    _demo_skills(db, rnd, today, users)
    _demo_growth(db, rnd, today, users)
    _demo_recruit(db, rnd, today, users)
    _demo_perform(db, rnd, today, users)
    _demo_talent(db, rnd, today, users)
    _demo_relations(db, today, users)
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


def _demo_skills(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Skill levels by length of service, a few classes, one new starter, one probation review."""
    from ..models import Course, EmployeeSkill, Skill
    from ..services import newstaff, skills

    hr = users["hr.officer"]
    active = list(db.scalars(select(Employee).where(Employee.status == "Active", Employee.is_sample.is_(True))))
    # classes first (they raise levels), then the levels supervisors recorded
    loto = db.get(Course, "CR-SAF-02")
    ids = [e.employee_id for e in active if e.org_unit_code in ("PRD-DRY", "PRD-PCL") and rnd.random() < 0.7]
    skills.record_session(db, hr, loto, today - timedelta(days=330), ids, {}, "May Sample (SAMPLE)")
    fire = db.get(Course, "CR-SAF-03")
    sv = [e.employee_id for e in active if e.job_code in ("PRD-SV", "SAF-OFF", "MTN-TECH")]
    skills.record_session(db, hr, fire, today - timedelta(days=200), sv, {}, "Provincial fire office (SAMPLE)")
    ind = db.get(Course, "CR-IND-01")
    saf = db.get(Course, "CR-SAF-01")
    recent = [e.employee_id for e in active if 20 < (today - e.join_date).days < 365]
    skills.record_session(db, hr, ind, today - timedelta(days=21), recent, {}, "HR (SAMPLE)")
    skills.record_session(db, hr, saf, today - timedelta(days=21), recent, {}, "May Sample (SAMPLE)")
    flt = db.get(Course, "CR-FLT-01")
    wh = [e.employee_id for e in active if e.org_unit_code == "PRD-WH"][:3]
    skills.record_session(db, hr, flt, today - timedelta(days=700), wh[:1], {}, "Forklift school (SAMPLE)")  # lapses soon
    skills.record_session(db, hr, flt, today - timedelta(days=760), wh[1:2], {}, "Forklift school (SAMPLE)")  # lapsed
    skills.record_session(db, hr, flt, today - timedelta(days=90), wh[2:3], {}, "Forklift school (SAMPLE)")
    db.flush()
    for e in active:
        years = (today - e.join_date).days / 365
        for code, need in skills.required(db, e.job_code).items():
            es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == e.employee_id, EmployeeSkill.skill_code == code))
            if es is not None and es.skill.valid_months:
                continue  # licence levels come from the classes above
            if (today - e.join_date).days < 30:
                lvl = 0
            elif years < 0.5:
                lvl = rnd.choice([1, 1, 2])
            elif years < 2:
                lvl = rnd.choice([2, 3, 3, 3])
            else:
                lvl = rnd.choice([3, 3, 3, 4, 4, 2])
            if lvl == 0:
                continue
            if es is None:
                es = EmployeeSkill(employee_id=e.employee_id, skill_code=code, history=[])
                db.add(es)
            es.level = max(es.level or 0, lvl)
            es.assessed_on = today - timedelta(days=rnd.randint(10, 200))
            es.assessed_by = "Supervisor (SAMPLE)"
    db.flush()
    new = db.get(Employee, "YTOP984")
    if new is not None:
        case = newstaff.open_onboarding(db, new, hr)
        for i in case.items:
            if i["code"] in ("CONTRACT", "DOCS", "SCAN", "BPLUS", "PPE", "PERMIT"):
                newstaff.tick(db, case, hr, i["code"], True)
    newstaff.sync_reviews(db, today)
    db.flush()


def _demo_growth(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Two assessment rounds (last year closed, this one open), supervisor views and two development plans."""
    from ..models import Assessment
    from ..services import growth

    hr = users["hr.officer"]
    groups = ["Operator", "Staff", "Manager"]
    comps = list(growth.competencies(db))
    # each person gets a stable profile: a few natural strengths and a few areas to grow
    lean = {}
    for e in db.scalars(select(Employee).where(Employee.status == "Active", Employee.is_sample.is_(True))):
        lean[e.employee_id] = {c: rnd.choice([-1, 0, 0, 0, 1]) for c in comps}

    def answer(a: Assessment, shift: float) -> dict:
        out = {}
        for q in growth.questions_for(db, a.audience):
            v = 3 + lean.get(a.employee_id, {}).get(q.competency_code, 0) + shift + rnd.choice([-1, 0, 0, 1])
            v = max(1, min(5, round(v)))
            out[str(q.id)] = 6 - v if q.reverse else v
        return out

    old = growth.open_round(db, hr, f"{today.year - 1} round 1", today - timedelta(days=380), today - timedelta(days=360), groups)
    db.flush()
    for a in db.scalars(select(Assessment).where(Assessment.round_id == old.id)):
        if rnd.random() < 0.7:
            growth.save_answers(db, a, hr, answer(a, -0.3), True, "paper" if a.audience == "core" else "web")
            a.submitted_at = datetime.combine(old.closes_on, time(10))
    growth.close_round(db, hr, old)
    cur = growth.open_round(db, hr, f"{today.year} round 1", today - timedelta(days=12), today + timedelta(days=9), groups,
                            "Please answer by the closing date. HR can help with the paper form.")
    db.flush()
    sup = users.get("sup.dry")
    cur_list = list(db.scalars(select(Assessment).where(Assessment.round_id == cur.id).order_by(Assessment.employee_id)))
    for a in cur_list:
        if rnd.random() < 0.8:
            growth.save_answers(db, a, hr, answer(a, 0.2), True, "paper" if a.audience == "core" else "web")
            if sup and a.employee.org_unit_code == "PRD-DRY" and rnd.random() < 0.6:
                mine = a.scores or {}
                growth.manager_view(db, a, sup, {c: max(1, min(5, round(v + rnd.choice([-1, 0, 0, 0, 0, 1])))) for c, v in mine.items()},
                                    "Good progress this year. Let us talk about the handover and the next machine to learn. (SAMPLE)")
    db.flush()
    done = [a for a in cur_list if a.status == "submitted" and a.employee.org_unit_code == "PRD-DRY"]
    if sup and done:
        p = growth.create_plan(db, sup, done[0].employee, done[0])
        items = [dict(it, actions=[dict(x) for x in it["actions"]]) for it in p.items]
        for it in items:
            for act in it["actions"]:
                if act["type"] == "course":
                    act["course"] = "CR-LDR-01" if it["competency"].startswith(("LE", "PE")) else "CR-IND-01"
                act["due"] = (today + timedelta(days=60)).isoformat()
            it["actions"][0]["done"] = True
            it["progress"] = 30
        p.items = items
        p.career_interest = "TEAMLEAD"
        p.employee_note = "I would like to train new colleagues. (SAMPLE)"
        growth.agree(db, sup, p)
    if sup and len(done) > 1:
        growth.create_plan(db, sup, done[1].employee, done[1])
    db.flush()


def _demo_recruit(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Four manpower requests in different states with SAMPLE candidates at every stage."""
    from ..models import Application, Candidate, Requisition
    from ..services import recruit

    hr, mgr = users["hr.officer"], users["hr.manager"]

    def approve_all(req: Requisition) -> None:
        appr = db.get(ApprovalRequest, req.approval_id)
        while appr.status == "pending":
            step = appr.active_step
            who = next((u for u in users.values() if (step.approver_employee_id and u.employee_id == step.approver_employee_id)
                        or (not step.approver_employee_id and step.approver_role in (u.roles or []))), None)
            approvals.decide(db, appr, who, "approved", "OK (SAMPLE)")

    def back(a: Application, days_ago: int) -> None:
        """Spread the history over the past days so the dashboard has real-looking times."""
        a.applied_on = today - timedelta(days=days_ago)
        n = len(a.history)
        for i, h in enumerate(a.history):
            d = today - timedelta(days=max(days_ago - i * max(days_ago // max(n, 1), 1), 0))
            h["at"] = datetime.combine(d, time(9 + i)).isoformat(timespec="minutes")
        a.history = list(a.history)

    people = [
        ("Somchai", "สมชาย", "M", "Thai", "walk_in"), ("Malee", "มาลี", "F", "Thai", "facebook"), ("Aung Min", None, "M", "Myanmar", "agency"),
        ("Suda", "สุดา", "F", "Thai", "referral"), ("Wichai", "วิชัย", "M", "Thai", "job_board"), ("Thida", None, "F", "Myanmar", "agency"),
        ("Anan", "อนันต์", "M", "Thai", "walk_in"), ("Kanya", "กันยา", "F", "Thai", "facebook"), ("Prasit", "ประสิทธิ์", "M", "Thai", "school"),
        ("Zaw Lin", None, "M", "Myanmar", "referral"), ("Ratana", "รัตนา", "F", "Thai", "job_board"), ("Boon", "บุญ", "M", "Thai", "walk_in"),
        ("Nilar", None, "F", "Myanmar", "agency"), ("Kittipong", "กิตติพงษ์", "M", "Thai", "facebook"),
    ]
    cands = []
    for i, (en, th, g, nat, src) in enumerate(people):
        bd = date(today.year - rnd.randint(19, 42), rnd.randint(1, 12), rnd.randint(1, 28))
        if i == 6:
            bd = date(today.year - 17, 3, 3)  # juvenile: flagged
        c = recruit.save_candidate(db, hr, {
            "full_name_en": f"{en} Applicant (SAMPLE)", "full_name_th": f"{th} ผู้สมัคร (SAMPLE)" if th else None, "title": "Mr." if g == "M" else "Ms.",
            "gender": g, "nationality": nat, "birth_date": bd, "phone": f"0899{i:06d}", "home_area": rnd.choice(["Mueang, Surat Thani", "Phunphin, Surat Thani", "Kanchanadit, Surat Thani"]),
            "education": rnd.choice(["Primary school (Grade 6)", "Lower secondary (Grade 9)", "Vocational certificate (ปวช.)", "High vocational (ปวส.), mechanics"]),
            "experience_years": rnd.randint(0, 8), "experience": "Factory operator, another company (SAMPLE)", "skills": rnd.choice(["Forklift licence", "Basic Thai reading", "Welding", ""]),
            "source": src, "referred_by": "YTOP951" if src == "referral" else None,
        }, consent="paper", talent_pool=(i % 4 == 0))
        cands.append(c)

    # A: filled earlier, the hire is YTOP984 (onboarding demo)
    a_req = recruit.request_people(db, hr, {"org_unit_code": "PRD-TRL", "position_id": "POS-0021", "job_code": "OPR-TRL", "employee_group": "Operator",
                                             "headcount": 1, "reason": "replacement", "replaces_employee_id": "YTOP986", "needed_by": today + timedelta(days=20)})
    approve_all(a_req)
    a_req.needed_by = today - timedelta(days=20)
    a_req.opened_on = today - timedelta(days=55)
    new = db.get(Employee, "YTOP984")
    hired_c = recruit.save_candidate(db, hr, {"full_name_en": new.full_name_en, "full_name_th": new.full_name_th, "gender": new.gender, "title": new.title,
                                               "nationality": new.nationality, "birth_date": new.birth_date, "phone": "0899123456", "source": "walk_in"}, consent="paper")
    ap = recruit.apply(db, hr, hired_c, a_req)
    ap.stage, ap.employee_id, ap.offer_status, ap.start_date = "hired", "YTOP984", "accepted", new.join_date
    ap.history = [*ap.history, {"stage": "screen", "by": hr.label, "note": ""}, {"stage": "interview", "by": hr.label, "note": ""},
                  {"stage": "offer", "by": hr.label, "note": "Offer accepted"}, {"stage": "hired", "by": hr.label, "note": "YTOP984"}]
    back(ap, 50)
    for c, stage, why in ((cands[11], "rejected", "skills"), (cands[13], "withdrawn", "pay")):
        x = recruit.apply(db, hr, c, a_req)
        recruit.move(db, hr, x, stage, "", why, force=True)
        back(x, 48)
    a_req.hired, a_req.status, a_req.closed_on = 1, "filled", new.join_date

    # B: Drying & Packing, 2 operators (1 seat free: outside budget, GM approved), late
    sup = users["sup.dry"]
    b_req = recruit.request_people(db, sup, {"org_unit_code": "PRD-DRY", "position_id": "POS-0017", "job_code": "OPR-DRY", "employee_group": "Operator",
                                             "headcount": 2, "reason": "new", "needed_by": today + timedelta(days=30),
                                             "requirements": "Can work night shift; forklift licence is a plus"})
    approve_all(b_req)
    b_req.opened_on, b_req.needed_by = today - timedelta(days=38), today - timedelta(days=5)
    plan = [(0, "applied", 3), (1, "applied", 2), (2, "screen", 9), (3, "interview", 20), (4, "interview", 16), (5, "offer", 25),
            (6, "screen", 6), (7, "rejected", 30), (8, "withdrawn", 28), (12, "applied", 1)]
    for idx, stage, ago in plan:
        x = recruit.apply(db, hr, cands[idx], b_req)
        if stage == "rejected":
            recruit.move(db, hr, x, "screen")
            recruit.move(db, hr, x, "rejected", "Could not do night shift (SAMPLE)", "interview")
        elif stage == "withdrawn":
            recruit.move(db, hr, x, "withdrawn", "Found a job closer to home (SAMPLE)", "location")
        elif stage in ("screen", "interview", "offer"):
            recruit.move(db, hr, x, "screen")
        if stage in ("interview", "offer"):
            iv = recruit.schedule(db, hr, x, datetime.combine(today - timedelta(days=ago - 4), time(10)), "onsite", sup.employee_id)
            if idx != 4:  # one interview still without a scorecard
                sc = {q["criterion"]: rnd.choice([3, 3, 4, 4, 5]) for q in iv.questions}
                ev = {k: "Gave a clear example from the last job (SAMPLE)" for k in sc}
                recruit.score(db, sup, iv, sc, ev, "hire" if idx == 5 else "maybe", "Ready to start; needs forklift training (SAMPLE)")
                iv.done_at = iv.when + timedelta(hours=1)
        if stage == "offer":
            recruit.offer(db, hr, x, today + timedelta(days=10), "Day shift for the first month (SAMPLE)")
            x.offer_at = datetime.combine(today - timedelta(days=9), time(14))
        back(x, ago)
    db.flush()

    # C: Pre Cleaning replacement, open, an AI-read application form
    c_req = recruit.request_people(db, users["sup.pcl"], {"org_unit_code": "PRD-PCL", "position_id": "POS-0018", "job_code": "OPR-PCL", "employee_group": "Operator",
                                                          "headcount": 1, "reason": "replacement", "replaces_employee_id": "YTOP987", "needed_by": today + timedelta(days=20)})
    approve_all(c_req)
    c_req.opened_on = today - timedelta(days=6)
    ai_c = recruit.read_cv(db, hr, _sample_form_png(), "image/png", "application-form.png", "walk_in")
    x = recruit.apply(db, hr, ai_c, c_req)  # no consent yet: blocked until HR records it
    back(x, 1)
    for idx in (9, 10):
        x = recruit.apply(db, hr, cands[idx], c_req)
        back(x, 4)

    # D: QA inspector, new headcount, waiting for the GM
    d_req = recruit.request_people(db, users["qa.head"], {"org_unit_code": "QA", "position_id": "POS-0013", "job_code": "QA-INSP", "employee_group": "Staff",
                                                           "headcount": 1, "reason": "new", "needed_by": today + timedelta(days=45),
                                                           "requirements": "Bachelor in science or chemistry; reads English test methods"})
    appr = db.get(ApprovalRequest, d_req.approval_id)
    while appr.status == "pending" and appr.active_step.name != "gm":
        step = appr.active_step
        who = next(u for u in users.values() if (step.approver_employee_id and u.employee_id == step.approver_employee_id) or (not step.approver_employee_id and step.approver_role in (u.roles or [])))
        approvals.decide(db, appr, who, "approved", "OK (SAMPLE)")

    # an old candidate past the PDPA retention period
    old = recruit.save_candidate(db, hr, {"full_name_en": "Old Applicant (SAMPLE)", "nationality": "Thai", "source": "walk_in", "phone": "0899777777"}, consent="paper")
    old.retain_until = today - timedelta(days=20)
    db.flush()


def _demo_perform(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Last year closed with ratings; this year in review with goals, actuals and appraisals at every step."""
    from ..models import Appraisal, Goal
    from ..services import perform as P

    hrm = users["hr.manager"]
    groups = ["Operator", "Staff", "Manager", "Japanese Management"]
    y = today.year

    def item(title, kpi, unit, direction, target, weight, actual=None, goal=None, measure=None):
        return {"id": uuid.uuid4().hex[:8], "title": title, "kpi_code": kpi, "goal_id": goal, "measure": measure, "unit": unit,
                "direction": direction, "target": target, "weight": weight, "actual": actual}

    def staff_items(a, goals, close=0.0):
        """Four KPIs; close > 0 fills actuals near the target."""
        out = []
        g = goals[0] if goals else None
        base = [("Own share of the unit goal" if g else "Main job output vs plan", g.kpi_code if g else "KPI-GEN-02", "%", "up", 100, 40, g.id if g else None),
                ("Improvement ideas put in place", "KPI-GEN-01", "ideas", "up", 4, 20, None),
                ("Project milestones on time", "KPI-GEN-02", "%", "up", 90, 20, None),
                ("Reports and records on time", None, "%", "up", 95, 20, None)]
        for t, k, u, d, tg, w, gid in base:
            act = round(tg * (close + rnd.uniform(-0.15, 0.2)), 1) if close else None
            out.append(item(t, k, u, d, tg, w, act, gid, "Monthly record kept by the owner"))
        return out

    # ---- last year: closed ----
    old, _ = P.open_cycle(db, hrm, f"{y - 1} performance year", date(y - 1, 1, 1), date(y - 1, 12, 31), date(y - 1, 2, 15), date(y - 1, 7, 15),
                          date(y, 1, 20), groups)
    for k, title, unit, target, actual in (("KPI-PRD-01", "Output vs plan", None, 100, 98.2), ("KPI-SAF-01", "Lost-time injuries", None, 0, 2),
                                           ("KPI-QA-01", "STR 20 lots passing first test", None, 97, 97.8)):
        db.add(Goal(cycle_id=old.id, level="company", kpi_code=k, title=title, target=target, actual=actual, actual_on=date(y - 1, 12, 31), weight=30, owner="GM"))
    pick = [5] * 2 + [4] * 6 + [3] * 18 + [2] * 3 + [1]
    for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id == old.id)):
        r = rnd.choice(pick)
        if a.form == "staff":
            a.items = [dict(i, mgr=max(1, min(5, r + rnd.choice([-1, 0, 0, 1])))) for i in staff_items(a, [], 1.0)]
        else:
            a.parts = {k: {"score": max(1, min(5, r + rnd.choice([-1, 0, 0, 1])))} for k in a.parts}
        a.competencies = {c: {"mgr": max(1, min(5, r + rnd.choice([-1, 0, 0, 1])))} for c in a.competencies}
        P.compute(a)
        a.status, a.mgr_at, a.mgr_by = "calibration", datetime(y, 1, 12, 10), "demo seed"
    old.status = "calibration"
    db.flush()
    P.release(db, hrm, old)
    old.released_at = datetime(y, 2, 5, 9)
    for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id == old.id, Appraisal.form == "staff")):
        if rnd.random() < 0.7:
            a.ack_at, a.ack_agree = datetime(y, 2, 9, 14), rnd.random() < 0.9

    # ---- this year: reviews open ----
    cur, _ = P.open_cycle(db, hrm, f"{y} performance year", date(y, 1, 1), date(y, 12, 31), date(y, 2, 15), date(y, 7, 15), date(y + 1, 1, 20), groups)
    cur.note = "SAMPLE: the demo opens the reviews early so every step can be tried."
    goal = {}
    for key, lvl, unit, k, title, target, actual, w in (
        ("c1", "company", None, "KPI-PRD-01", "Output vs plan", 100, 97.1, 30),
        ("c2", "company", None, "KPI-SAF-01", "Zero lost-time injuries", 0, 1, 30),
        ("c3", "company", None, "KPI-QA-01", "STR 20 lots passing first test", 98, 98.6, 40),
        ("prd", "unit", "PRD", "KPI-PRD-01", "Production output vs plan", 100, 96.0, 50),
        ("prd2", "unit", "PRD", "KPI-PRD-03", "Energy per ton", 160, 152, 50),
        ("dry", "unit", "PRD-DRY", "KPI-PRD-02", "Dryer unplanned downtime (hours, year)", 120, 98, 60),
        ("dry2", "unit", "PRD-DRY", "KPI-PRD-05", "Bale weight within tolerance", 99, 99.4, 40),
        ("pcl", "unit", "PRD-PCL", "KPI-SAF-03", "5S audit score, pre-cleaning", 85, 78, 100),
        ("qa", "unit", "QA", "KPI-QA-02", "Customer complaints", 3, 2, 60),
        ("lab", "unit", "LAB", "KPI-QA-03", "Lab results on time", 95, 96.5, 100),
        ("mtn", "unit", "MTN", "KPI-MTN-01", "Preventive maintenance on time", 95, 91, 100),
        ("hr", "unit", "HR", "KPI-HR-01", "Attendance rate", 97, 96.2, 50),
        ("hr2", "unit", "HR", "KPI-HR-06", "Days to fill a vacancy", 30, 34, 50),
        ("rmr", "unit", "RMR", "KPI-RMR-01", "Raw material weighing variance", 0.5, 0.4, 100),
    ):
        g = Goal(cycle_id=cur.id, level=lvl, org_unit_code=unit, kpi_code=k, title=title, target=target, actual=actual,
                 actual_on=today - timedelta(days=rnd.randint(3, 25)), weight=w, owner="SAMPLE owner")
        db.add(g)
        db.flush()
        goal[key] = g
    for key in ("prd", "prd2", "dry", "dry2", "pcl"):
        goal[key].parent_id = goal["c1"].id
    goal["qa"].parent_id = goal["lab"].parent_id = goal["c3"].id
    rows = {a.employee_id: a for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id == cur.id))}
    for a in rows.values():
        if a.form != "staff":
            continue
        gs = P.unit_goals(db, cur, a.org_unit_code)
        if a.employee_id == "YTR815":  # still drafting, past the due date
            a.items = staff_items(a, gs)[:2]
            continue
        a.items = staff_items(a, gs)
        a.status, a.goals_agreed_at = "goals_agreed", datetime(y, 2, rnd.randint(5, 14), 10)
        a.items = [dict(i, mid="On track (SAMPLE)") for i in a.items]
        a.midyear_note, a.midyear_at = "Talked through progress; keep the focus on the first KPI. (SAMPLE)", datetime(y, 7, 14, 15)

    cur.status = "review"

    def self_done(a, send=True):
        a.items = [dict(i, actual=round(i["target"] * rnd.uniform(0.9, 1.2), 1), self=rnd.choice([3, 3, 4]), self_comment="Kept the monthly record. (SAMPLE)")
                   for i in a.items]
        a.competencies = {c: {"self": rnd.choice([3, 4, 4])} for c in a.competencies}
        a.self_note = "I would like more training on data and Excel. (SAMPLE)"
        a.status = "manager" if send else "self"
        if send:
            a.self_at = datetime.combine(today - timedelta(days=4), time(9))

    def mgr_done(a, u):
        form = {f"cm_{c}": str(rnd.choice([3, 3, 4, 4, 5])) for c in a.competencies}
        form["mgr_note"] = "Solid year; steady with the records, can lead more. (SAMPLE)"
        if a.form == "operator":
            P.collect_facts(db, a, today)
            sug = P.suggest_parts(db, a)
            for k, v in sug.items():
                if not v and k != "behaviour":
                    form[f"p_{k}"] = "3"
        P.manager_review(db, u, a, form, True)

    for eid in ("YTR806", "YTR802", "YTR813", "YTR814"):
        if eid in rows:
            self_done(rows[eid])
    if "YTR807" in rows:
        self_done(rows["YTR807"], send=False)
    qa = users.get("qa.head")
    for eid in ("YTR813", "YTR814"):
        if qa and eid in rows and rows[eid].reviewer_employee_id == qa.employee_id:
            mgr_done(rows[eid], qa)
    sup = users.get("sup.dry")
    dry = sorted(e for e, a in rows.items() if a.form == "operator" and a.org_unit_code == "PRD-DRY" and a.reviewer_employee_id == "YTR806")
    for eid in dry[:7]:
        P.skip_self(db, sup, rows[eid], "No login: talked it through at the line meeting (SAMPLE)")
        mgr_done(rows[eid], sup)
    head = users.get("prod.head")
    for eid in dry[:3]:
        a = rows[eid]
        if head and a.status == "endorse" and a.endorser_employee_id == head.employee_id:
            P.endorse(db, head, a, "Agree. (SAMPLE)", a.proposed_rating)
    db.flush()


def _sample_form_png() -> bytes:
    """A drawn stand-in for a photographed paper application form (SAMPLE)."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((30, 30, 870, 1170), outline="black", width=3)
    lines = ["JOB APPLICATION FORM (SAMPLE)", "", "Name: Somchai Sample", "Nickname: Chai", "Birth date: 14/05/2544",
             "Phone: 089-000-0123   LINE: chai.sample", "Lives in: Mueang, Surat Thani", "Education: Vocational cert., mechanics",
             "Experience: 3 years", "2023-2026 Machine operator, a sawmill", "2022 Helper, a palm oil mill",
             "Skills: forklift licence, basic welding", "", "[x] I have read the privacy notice", "Signature: ________________"]
    for i, t in enumerate(lines):
        d.text((70, 70 + i * 62), t, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _demo_talent(db: Session, rnd: random.Random, today: date, users: dict) -> None:
    """Last year's talent review: most people proposed by their manager, about half agreed in the meeting."""
    from ..models import Appraisal, PerformanceCycle, TalentReview
    from ..models.talent import BOXES, box_of, perf_level

    by_emp = {u.employee_id: u for u in users.values() if u.employee_id}
    old = db.scalar(select(PerformanceCycle).where(PerformanceCycle.released_at.is_not(None)).order_by(PerformanceCycle.period_end.desc()))
    if old is None:
        return
    reasons = ["Offered a job nearer home (SAMPLE)", "Wants a team leader role soon (SAMPLE)", "Pay gap with nearby factories (SAMPLE)"]
    for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id == old.id, Appraisal.final_rating.is_not(None)).order_by(Appraisal.employee_id)):
        if a.form == "operator" and rnd.random() < 0.6:
            continue  # not every operator reviewed yet
        roll = rnd.random()
        if roll < 0.12:
            continue  # not started
        perf = perf_level(a.final_rating)
        lean = {3: [2, 3, 3], 2: [1, 2, 2, 3], 1: [1, 1, 2]}[perf]
        ans = {k: rnd.choice(lean) for k in ("learn", "scope", "lead")}
        avg = sum(ans.values()) / 3
        pot = 3 if avg >= 2.5 else 2 if avg >= 1.5 else 1
        risk = rnd.choice(["low", "low", "low", "medium", "medium", "high"])
        who = by_emp.get(a.reviewer_employee_id)
        t = TalentReview(year=old.period_end.year, employee_id=a.employee_id, org_unit_code=a.org_unit_code, appraisal_id=a.id, rating=a.final_rating,
                         performance=perf, answers=ans, potential=pot, box=box_of(perf, pot)[0], risk=risk,
                         risk_reason=rnd.choice(reasons) if risk == "high" else None,
                         next_move={3: "ready_2y", 2: "stay", 1: "stay"}[pot] if not (perf == 3 and pot == 3) else rnd.choice(["ready_now", "ready_2y"]),
                         action=BOXES[(perf, pot)][2] + " (SAMPLE)", status="proposed" if roll < 0.55 else "agreed",
                         proposed_by=who.label if who else "hr.manager", proposed_at=datetime(today.year, 2, 20, 10))
        if roll < 0.2:
            t.status, t.proposed_at = "draft", None
        if t.status == "agreed":
            t.agreed_by, t.agreed_at = users["hr.manager"].label, datetime(today.year, 3, 3, 14)
        db.add(t)
    db.flush()


def _demo_relations(db: Session, today: date, users: dict) -> None:
    """Employee relations cases at every step (SAMPLE stories)."""
    from ..models import ErCase
    from ..services.common import next_ref

    hro, hrm, gm = users["hr.officer"].label, users["hr.manager"].label, users["gm"].label
    d = lambda n: today - timedelta(days=n)  # noqa: E731

    def case(kind, cat, emp, inc, summary, status, other=None, **kw):
        c = ErCase(ref=next_ref(db, "ER", d(inc).year), kind=kind, category=cat, employee_id=emp, other_employee_id=other,
                   org_unit_code=db.get(Employee, emp).org_unit_code, incident_on=d(inc), reported_on=d(inc - 1), reported_by=kw.pop("by", hro),
                   summary=summary, confidential=kind == "harassment", status=status,
                   reply_due=d(inc - 1) + timedelta(days=15) if kind != "discipline" else None,
                   log=[{"at": d(inc - 1).isoformat() + "T09:00", "by": hro, "text": "Case opened"}], **kw)
        db.add(c)
        return c

    case("discipline", "attendance", "YTOP955", 120, "Absent without leave 3 working days in a row; no call to the supervisor. (SAMPLE)", "closed",
         statement="Family emergency in Myanmar, phone had no signal. (SAMPLE)", statement_on=d(116), findings="Scan records confirm 3 days absent; no leave request. (SAMPLE)",
         outcome="written", reasons="Absent without leave on 3 working days, breaking work rule 5.2 on reporting absence. (SAMPLE)",
         proposed_by=hro, proposed_at=datetime.combine(d(112), datetime.min.time()), decided_by=hrm, decided_at=datetime.combine(d(110), datetime.min.time()),
         warning_until=d(120) + timedelta(days=365), letter_given_on=d(109), employee_signed=True, closed_at=datetime.combine(d(109), datetime.min.time()))
    case("discipline", "attendance", "YTOP955", 6, "Absent without leave again for 2 days after the written warning. (SAMPLE)", "investigating",
         statement="Was sick but did not inform anyone. No medical certificate. (SAMPLE)", statement_on=d(3))
    case("discipline", "safety", "YTOP963", 14, "Worked on the cutter without the guard in place, seen by the safety officer. (SAMPLE)", "decision",
         statement="The guard was slowing the line; did not think it was dangerous. (SAMPLE)", statement_on=d(10),
         findings="Safety officer photo and line leader witness. Work rule 7.1 machine guards. (SAMPLE)", outcome="written",
         reasons="Ran the cutter with the guard removed on the date above, breaking work rule 7.1. (SAMPLE)", proposed_by=hro,
         proposed_at=datetime.combine(d(2), datetime.min.time()))
    case("discipline", "quality", "YTOP970", 3, "Bales packed without weighing for one lot; 4 bales under weight found by QA. (SAMPLE)", "open",
         by=users["sup.pcl"].label)
    case("grievance", "workload", "YTOP975", 25, "Says OT is always given to the same people and asked to explain how OT is shared. (SAMPLE)", "investigating",
         findings="OT records for 3 months pulled; checking the rotation list with the line leader. (SAMPLE)")
    case("harassment", "harassment", "YTR813", 9, "Complaint about repeated unwanted comments by a colleague. (SAMPLE)", "investigating",
         other="YTR808", by=hrm, statement="Interviewed both people separately. (SAMPLE)", statement_on=d(6))
    case("grievance", "treatment", "YTOP980", 70, "Felt shouted at by the line leader during a breakdown. (SAMPLE)", "closed",
         findings="Two witnesses: voice raised once during the breakdown, apology given. (SAMPLE)", outcome="partly",
         reasons="Talked with the line leader about calm communication; explained to the employee. (SAMPLE)", proposed_by=hro,
         proposed_at=datetime.combine(d(60), datetime.min.time()), decided_by=hrm, decided_at=datetime.combine(d(58), datetime.min.time()),
         letter_given_on=d(57), closed_at=datetime.combine(d(57), datetime.min.time()))
    db.flush()
