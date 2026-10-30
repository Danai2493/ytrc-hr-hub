from datetime import date, datetime, time, timedelta

from app.models import Employee, OTRequest
from app.services.common import normalize_employee_id, valid_employee_id
from app.services.facescan import employee_to_scan_user, parse_lines, scan_user_to_employee
from app.services.timecalc import check_ot, classify, combine_ot_times, payroll_period, shift_for


def next_weekday(d: date, wd: int) -> date:
    return d + timedelta(days=(wd - d.weekday()) % 7)


def test_ids():
    assert normalize_employee_id("ytr-9") == "YTR009"
    assert normalize_employee_id(" YTOP 951 ") == "YTOP951"
    assert valid_employee_id("YTOP951", "Operator")
    assert not valid_employee_id("YTR951", "Operator")
    assert not valid_employee_id("YTOP951", "Staff")
    assert valid_employee_id("YTR009", "Manager")


def test_scan_user_mapping():
    assert scan_user_to_employee("0000001009") == "YTR009"
    assert scan_user_to_employee("0000002898") == "YTOP898"
    assert scan_user_to_employee("0000003029") is None
    assert employee_to_scan_user("YTOP951") == "0000002951"


def test_parse_scan_lines():
    punches, bad = parse_lines(["0000002898  16-10-2026  06:58  1  I", "garbage", "0000001009,17/10/2026,17:05,1,I"])
    assert punches == [("0000002898", datetime(2026, 10, 16, 6, 58)), ("0000001009", datetime(2026, 10, 17, 17, 5))]
    assert bad == ["garbage"]


def test_payroll_periods():
    assert payroll_period("OPERATOR_HALF", date(2026, 10, 3)) == (date(2026, 10, 1), date(2026, 10, 15))
    assert payroll_period("OPERATOR_HALF", date(2026, 10, 31)) == (date(2026, 10, 16), date(2026, 10, 31))
    assert payroll_period("OPERATOR_HALF", date(2026, 2, 20)) == (date(2026, 2, 16), date(2026, 2, 28))
    assert payroll_period("STAFF_MONTHLY", date(2026, 10, 5)) == (date(2026, 9, 21), date(2026, 10, 20))
    assert payroll_period("STAFF_MONTHLY", date(2026, 10, 25)) == (date(2026, 10, 21), date(2026, 11, 20))
    assert payroll_period("STAFF_MONTHLY", date(2026, 12, 25)) == (date(2026, 12, 21), date(2027, 1, 20))


def test_workday_ot_is_x15(db):
    emp = db.get(Employee, "YTR813")  # QA, factory day shift 07-16
    d = next_weekday(date.today() + timedelta(days=7), 1)  # a Tuesday
    f, t = combine_ot_times(d, time(16), time(18), shift_for(db, emp, d))
    kind, split, inside = classify(db, emp, d, f, t)
    assert kind == "workday" and split == {"OT15": 120} and inside == 0


def test_ot_inside_shift_is_not_ot(db):
    """Real step 3 finding: 06:30-08:00 written for a night shift that ends at 07:00."""
    emp = db.get(Employee, "YTR813")
    emp.shift_code = "PRD-N"
    d = next_weekday(date.today() + timedelta(days=7), 1)
    f, t = combine_ot_times(d, time(6, 30), time(8), shift_for(db, emp, d))
    assert f.date() == d + timedelta(days=1)  # belongs to the next morning
    kind, split, inside = classify(db, emp, d, f, t)
    assert split == {"OT15": 60} and inside == 30


def test_holiday_split_monthly_vs_daily(db):
    from app.models import Calendar

    emp = db.get(Employee, "YTR813")
    rest = min(db.get(Calendar, emp.calendar_code).rest_days)  # Factory rests on Friday
    rest_day = next_weekday(date.today() + timedelta(days=7), rest)
    f, t = combine_ot_times(rest_day, time(7), time(18), shift_for(db, emp, rest_day))
    kind, split, _ = classify(db, emp, rest_day, f, t)
    assert kind == "holiday" and split == {"HOL10": 480, "OT30": 120}
    emp.pay_type = "Daily"
    _, split, _ = classify(db, emp, rest_day, f, t)
    assert split == {"HOL20": 480, "OT30": 120}


def test_juvenile_blocked(db):
    emp = db.get(Employee, "YTOP957")
    d = next_weekday(date.today() + timedelta(days=7), 1)
    sh = shift_for(db, emp, d)
    f, t = combine_ot_times(d, sh.end, time(19), sh)
    findings, _, _ = check_ot(db, emp, d, f, t)
    assert any(x.code == "juvenile_ot" and x.level == "block" for x in findings)


def test_weekly_limit_blocks_and_override_flags(db):
    emp = db.get(Employee, "YTR814")
    monday = next_weekday(date.today() + timedelta(days=14), 0)
    for i in range(5):
        d = monday + timedelta(days=i)
        db.add(OTRequest(ref=f"T-{i}", employee_id=emp.employee_id, work_date=d, ot_from=datetime.combine(d, time(16)), ot_to=datetime.combine(d, time(23)), requested_minutes=420, status="approved"))
    db.flush()
    d = monday + timedelta(days=5)  # Saturday, a workday on the factory calendar
    f, t = datetime.combine(d, time(16)), datetime.combine(d, time(19))
    findings, _, _ = check_ot(db, emp, d, f, t)
    assert any(x.code == "weekly_limit" and x.level == "block" for x in findings)
    findings, _, _ = check_ot(db, emp, d, f, t, override=True)
    assert any(x.code == "weekly_limit" and x.level == "flag" for x in findings)


def test_overlap_and_after_exit(db):
    emp = db.get(Employee, "YTOP990")  # resigned in the demo data
    d = emp.exit_date + timedelta(days=3)
    f, t = datetime.combine(d, time(17)), datetime.combine(d, time(19))
    findings, _, _ = check_ot(db, emp, d, f, t)
    assert any(x.code == "after_exit" for x in findings)


def test_ytrc_vacation_policy(db):
    from datetime import date

    from app.models import Employee, LeaveRequest, LeaveType
    from app.services import leave

    e = Employee(employee_id="YTOP999", employee_group="Operator", full_name_en="Policy test (SAMPLE)", join_date=date(2019, 6, 1), org_unit_code="PRD-DRY")
    assert leave._vac_own(db, e, 2019) == 3  # joined June: 7 full months, 1 day per 2 months
    assert leave._vac_own(db, e, 2020) == 6  # "1 year or less" in the company table
    assert leave._vac_own(db, e, 2021) == 7
    assert leave._vac_own(db, e, 2030) == 11  # +5 cap
    e.join_date = date(2019, 6, 15)
    assert leave._vac_own(db, e, 2019) == 3
    # within probation: earned but not usable
    e.join_date = date(2026, 6, 1)
    f, _ = leave.check(db, e, db.get(LeaveType, "VAC"), date(2026, 8, 3), date(2026, 8, 3), today=date(2026, 7, 1))
    assert any(x.code == "leave_probation" and x.level == "block" for x in f)


def test_vacation_carry_forward_capped(db):
    from datetime import date

    from app.models import Employee
    from app.services import leave

    e = Employee(employee_id="YTOP998", employee_group="Operator", full_name_en="Carry test (SAMPLE)", join_date=date(2015, 1, 5), org_unit_code="PRD-DRY")
    # 2026 own = 11, nothing used in 2026 -> only 3 carry into 2027 (first carry year)
    assert leave.vacation_carry(db, e, 2026) == 0
    assert leave.vacation_carry(db, e, 2027) == 3
    assert leave.vacation_entitlement(db, e, 2027) == 14
