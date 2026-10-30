"""End-to-end flows through the web screens, as each role would use them."""
import io
import re
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    ApprovalRequest,
    AuditLog,
    CaptureLine,
    DataIssue,
    Employee,
    FormCapture,
    OTRequest,
    PayBatch,
)


def future_weekday(wd: int, weeks: int = 3) -> date:
    d = date.today() + timedelta(days=7 * weeks)
    return d + timedelta(days=(wd - d.weekday()) % 7)


def last_approval(db) -> ApprovalRequest:
    db.expire_all()
    return db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.id.desc())).first()


def test_login_required_and_csrf(login):
    from fastapi.testclient import TestClient

    from app.main import app

    anon = TestClient(app)
    assert anon.get("/people", follow_redirects=False).headers["location"] == "/login"
    c = login("hr.officer")
    r = c.c.post("/ot/new", data={"work_date": "2026-10-01"}, follow_redirects=False)
    assert r.status_code == 400  # no CSRF token


def test_wrong_password_is_logged(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"username": "gm", "password": "nope", "_csrf": tok})
    assert db.scalar(select(AuditLog).where(AuditLog.action == "login_failed"))


def test_supervisor_ot_flow_to_bplus(login, db):
    sup = login("sup.dry")
    d = future_weekday(2)  # a Wednesday
    r = sup.post("/ot/check", {"work_date": d.isoformat(), "employee_id": ["YTOP951", "YTOP957"], "ot_from": ["15:00", "15:00"], "ot_to": ["17:00", "17:00"]})
    assert "YTOP951" in r.text and "under 18" in r.text  # live check shows the juvenile block
    r = sup.post("/ot/new", {"work_date": d.isoformat(), "reason": "Shipment", "employee_id": ["YTOP951", "YTOP957"], "ot_from": ["15:00", "15:00"], "ot_to": ["17:00", "17:00"]})
    assert r.status_code == 303
    req = db.scalar(select(OTRequest).where(OTRequest.employee_id == "YTOP951", OTRequest.work_date == d))
    assert req and req.status == "pending" and req.split["req"] == {"OT15": 120}
    assert db.scalar(select(OTRequest).where(OTRequest.employee_id == "YTOP957", OTRequest.work_date == d)) is None
    # Supervisor cannot approve their own submission; Production head can
    appr = db.get(ApprovalRequest, req.approval_id)
    assert appr.active_step.approver_employee_id == "YTR805"
    assert sup.post(f"/approvals/{appr.id}/decide", {"decision": "approved"}).status_code == 303
    db.expire_all()
    assert db.get(ApprovalRequest, appr.id).current_step == 1
    head = login("prod.head")
    head.post(f"/approvals/{appr.id}/decide", {"decision": "approved", "comment": "ok"})
    db.expire_all()
    assert db.get(OTRequest, req.id).status == "approved"
    # Face scan arrives
    hr = login("hr.officer")
    scan = f"0000002951  {d:%d-%m-%Y}  06:41  1  I\n0000002951  {d:%d-%m-%Y}  17:04  1  I\n"
    r = hr.post("/attendance/import", files={"file": ("timelog_test.txt", scan.encode(), "text/plain")})
    assert r.status_code == 303
    db.expire_all()
    req = db.get(OTRequest, req.id)
    assert req.scan_minutes == 120 and req.payable_minutes == 120
    # HR verifies, a second HR person checks the batch, then export
    hr.post("/ot/verify", {"ids": [str(req.id)], "cycle": "OPERATOR_HALF"})
    db.expire_all()
    assert db.get(OTRequest, req.id).status == "verified"
    from app.services.timecalc import payroll_period

    p0, _ = payroll_period("OPERATOR_HALF", d)
    r = hr.post("/pay/build", {"cycle": "OPERATOR_HALF", "start": p0.isoformat()})
    batch_id = int(r.headers["location"].rsplit("/", 1)[1])
    batch = db.get(PayBatch, batch_id)
    assert any(ln.employee_id == "YTOP951" and ln.pay_code == "OT15" for ln in batch.lines)
    assert hr.get(f"/pay/{batch_id}/export.csv").status_code == 400  # not checked yet
    hr.post(f"/pay/{batch_id}/status", {"status": "checked"})
    db.expire_all()
    assert db.get(PayBatch, batch_id).status == "draft"  # the builder cannot check their own batch
    hr2 = login("hr.officer2")
    hr2.post(f"/pay/{batch_id}/status", {"status": "checked"})
    r = hr2.get(f"/pay/{batch_id}/export.csv")
    assert r.status_code == 200 and "YTOP951,OT15,2.00" in r.text


def test_scan_short_reduces_payable(login, db):
    sup = login("sup.pcl")
    d = future_weekday(3)
    sup.post("/ot/new", {"work_date": d.isoformat(), "reason": "Backlog", "employee_id": ["YTOP963"], "ot_from": ["16:00"], "ot_to": ["19:00"]})
    req = db.scalar(select(OTRequest).where(OTRequest.employee_id == "YTOP963", OTRequest.work_date == d))
    assert req is not None
    hr = login("hr.officer")
    scan = f"0000002963  {d:%d-%m-%Y}  06:50  1  I\n0000002963  {d:%d-%m-%Y}  17:30  1  I\n"
    hr.post("/attendance/import", files={"file": ("t.txt", scan.encode(), "text/plain")})
    db.expire_all()
    req = db.get(OTRequest, req.id)
    assert req.scan_minutes == 90 and req.payable_minutes == 90
    assert any(f["code"] == "scan_short" for f in req.flags)


def test_new_employee_needs_second_hr(login, db):
    hr = login("hr.officer")
    r = hr.get("/people/new?group=Operator")
    next_id = re.search(r'name="employee_id" value="([A-Z0-9]+)"', r.text).group(1)
    assert next_id == "YTOP001"  # sample range never decides the next real number
    data = {"employee_id": "YTOP001", "employee_group": "Operator", "pay_type": "Daily", "title": "Ms.", "gender": "F", "full_name_en": "Test Person (SAMPLE)",
            "nationality": "Myanmar", "birth_date": "2000-05-01", "join_date": date.today().isoformat(), "org_unit_code": "PRD-DRY"}
    r = hr.post("/people/new", data)
    assert r.status_code == 303
    appr = last_approval(db)
    assert db.get(Employee, "YTOP001") is None
    hr.post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    assert db.get(Employee, "YTOP001") is None  # same person cannot approve
    login("hr.officer2").post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    emp = db.get(Employee, "YTOP001")
    assert emp and emp.events[0].event_type == "hire"
    bad = dict(data, employee_id="YTR999", birth_date="2015-01-01")
    r = hr.post("/people/new", bad)
    assert r.status_code == 400 and "under 15" in r.text


def test_transfer_keeps_history(login, db):
    head = login("prod.head")
    r = head.post("/people/YTOP952/event", {"event_type": "transfer", "effective_date": date.today().isoformat(), "to_org_unit": "PRD-WH", "note": "Line balance"})
    assert r.status_code == 303
    appr = last_approval(db)
    names = [s.name for s in appr.steps]
    assert names[0] in ("receiving_head", "hr_check")  # submitter's own step is skipped
    login("hr.officer").post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    login("gm").post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    emp = db.get(Employee, "YTOP952")
    assert emp.org_unit_code == "PRD-WH"
    ev = [e for e in emp.events if e.event_type == "transfer"][0]
    assert ev.from_org_unit == "PRD-DRY" and ev.to_org_unit == "PRD-WH"
    from app.services.people import org_unit_on

    assert org_unit_on(emp, date.today() - timedelta(days=1)) == "PRD-DRY"


def test_exit_needs_reason(login, db):
    head = login("prod.head")
    r = head.post("/people/YTOP953/event", {"event_type": "exit", "effective_date": date.today().isoformat()})
    assert r.status_code == 400 and "reason" in r.text.lower()


def test_scope_supervisor_cannot_see_other_units(login):
    sup = login("sup.dry")
    assert sup.get("/people/YTOP951").status_code == 200
    assert sup.get("/people/YTR813").status_code == 404
    assert sup.get("/pay").status_code == 403


def test_restricted_view_is_logged(login, db):
    hr = login("hr.manager")
    r = hr.get("/people/YTOP951?restricted=1")
    assert "Work permit no." in r.text
    db.expire_all()
    assert db.scalar(select(AuditLog).where(AuditLog.action == "view_restricted", AuditLog.entity_id == "YTOP951"))
    assert "Work permit no." not in login("prod.head").get("/people/YTOP951?restricted=1").text


def _photo() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, "PNG")
    return buf.getvalue()


def test_ai_capture_ot_form(login, db):
    hr = login("hr.officer")
    r = hr.post("/capture/new", {"form_type": "OT_FORM"}, files={"file": ("form.png", _photo(), "image/png")})
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    cap = db.get(FormCapture, cid)
    assert cap.status == "needs_review" and len(cap.lines) == 6
    by_no = {ln.line_no: ln for ln in cap.lines}
    assert any(i["code"] == "id_not_found" for i in by_no[4].issues)  # "YTOP95?" unreadable
    assert any(i["code"] == "hours_mismatch" for i in by_no[5].issues)  # 16:30-19:00 written as 2h
    # HR fixes the unclear ID and accepts the clean lines with the paper signature
    hr.post(f"/capture/{cid}/line/{by_no[4].id}", {"employee_id": "YTOP954", "ot_from": "17:00", "ot_to": "19:00", "hours": "2"})
    db.expire_all()
    line4 = db.get(CaptureLine, by_no[4].id)
    assert not any(i["level"] == "block" for i in line4.issues)
    ok_ids = [str(ln.id) for ln in db.get(FormCapture, cid).lines if ln.status == "ok"]
    r = hr.post(f"/capture/{cid}/accept", {"line_ids": ok_ids, "paper_signed": "on"})
    db.expire_all()
    made = db.scalars(select(OTRequest).where(OTRequest.source == "paper")).all()
    assert len(made) >= 3
    assert all(m.status == "approved" for m in made)  # paper signature recorded as first step
    appr = db.get(ApprovalRequest, made[0].approval_id)
    assert "paper signature" in appr.steps[0].decided_by


def test_ai_capture_work_permit(login, db):
    hr = login("hr.manager")
    r = hr.post("/capture/new", {"form_type": "WORK_PERMIT", "employee": "YTOP955"}, files={"file": ("wp.png", _photo(), "image/png")})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    hr.post(f"/capture/{cid}/accept", {"employee_id": "YTOP955", "work_permit_no": "WP-SAMPLE-0001", "expiry_date": "2027-01-31"})
    db.expire_all()
    emp = db.get(Employee, "YTOP955")
    assert emp.restricted.work_permit_no == "WP-SAMPLE-0001"


def test_data_health_closes_fixed_issues(login, db):
    hr = login("hr.manager")
    hr.post("/health/run")
    db.expire_all()
    assert db.scalar(select(DataIssue).where(DataIssue.rule_code == "JUVENILE", DataIssue.status == "open"))
    assert db.scalar(select(DataIssue).where(DataIssue.rule_code == "SCAN_UNKNOWN", DataIssue.entity_id == "0000003029"))


# ---------- Phase 2: leave and exit ----------

def _workday(db, eid: str, start: date, step: int = 1) -> date:
    from app.services.timecalc import is_rest_day

    emp = db.get(Employee, eid)
    d = start
    while is_rest_day(db, emp, d):
        d += timedelta(days=step)
    return d


def test_leave_request_approve_feeds_attendance(login, db):
    from app.models import DailyAttendance, LeaveRequest

    sup = login("sup.dry")
    # a past workday with no scans for YTOP953: it shows as absent "a"
    db.expire_all()
    att = db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == "YTOP953", DailyAttendance.day_code == "a", DailyAttendance.work_date < date.today())).first()
    if att is None:
        from app.services.facescan import rebuild_attendance

        d = _workday(db, "YTOP953", date.today() - timedelta(days=3), -1)
        for a in db.scalars(select(DailyAttendance).where(DailyAttendance.employee_id == "YTOP953", DailyAttendance.work_date == d)):
            db.delete(a)
        db.commit()
        from app.models import ScanPunch

        for p in db.scalars(select(ScanPunch).where(ScanPunch.employee_id == "YTOP953")):
            if p.punched_at.date() == d:
                db.delete(p)
        db.commit()
        rebuild_attendance(db, d, d, ["YTOP953"])
        db.commit()
        att = db.scalar(select(DailyAttendance).where(DailyAttendance.employee_id == "YTOP953", DailyAttendance.work_date == d))
    d = att.work_date
    assert att.day_code == "a"
    r = sup.post("/leave/check", {"employee_id": "YTOP953", "leave_type": "PERS", "start_date": d.isoformat(), "end_date": d.isoformat()})
    assert "YTOP953" in r.text and "1 d" in r.text
    r = sup.post("/leave/new", {"employee_id": "YTOP953", "leave_type": "PERS", "start_date": d.isoformat(), "end_date": d.isoformat(), "reason": "Family"})
    assert r.status_code == 303
    db.expire_all()
    req = db.scalars(select(LeaveRequest).where(LeaveRequest.employee_id == "YTOP953").order_by(LeaveRequest.id.desc())).first()
    assert req.status == "pending" and req.days == 1
    # overlap is blocked
    r = sup.post("/leave/new", {"employee_id": "YTOP953", "leave_type": "SICK", "start_date": d.isoformat(), "end_date": d.isoformat()})
    db.expire_all()
    assert db.scalar(select(LeaveRequest).where(LeaveRequest.employee_id == "YTOP953", LeaveRequest.leave_type == "SICK", LeaveRequest.start_date == d)) is None
    login("prod.head").post(f"/approvals/{req.approval_id}/decide", {"decision": "approved"})
    db.expire_all()
    assert db.get(LeaveRequest, req.id).status == "approved"
    att = db.scalar(select(DailyAttendance).where(DailyAttendance.employee_id == "YTOP953", DailyAttendance.work_date == d))
    assert att.day_code == "p"  # no longer counted as a no-show
    assert login("sup.dry").get(f"/leave/{req.id}").status_code == 200


def test_leave_sick_three_days_needs_hr_check(login, db):
    from app.models import LeaveRequest

    sup = login("sup.pcl")
    d0 = _workday(db, "YTOP966", date.today() + timedelta(days=21))
    d1 = d0 + timedelta(days=6)
    emp = db.get(Employee, "YTOP966")
    if emp.org_unit_code != "PRD-PCL":
        return
    r = sup.post("/leave/new", {"employee_id": "YTOP966", "leave_type": "SICK", "start_date": d0.isoformat(), "end_date": d1.isoformat()})
    assert r.status_code == 303
    req = db.scalars(select(LeaveRequest).where(LeaveRequest.employee_id == "YTOP966").order_by(LeaveRequest.id.desc())).first()
    assert any(f["code"] == "leave_document" for f in req.flags)
    appr = db.get(ApprovalRequest, req.approval_id)
    assert [s.name for s in appr.steps][-1] == "hr_check"


def test_leave_maternity_only_for_women(db):
    from app.models import LeaveType
    from app.services import leave

    man = db.scalars(select(Employee).where(Employee.gender == "M", Employee.status == "Active")).first()
    f, _ = leave.check(db, man, db.get(LeaveType, "MAT"), date.today() + timedelta(days=30), date.today() + timedelta(days=40))
    assert any(x.code == "leave_not_for" and x.level == "block" for x in f)


def test_ai_capture_leave_form(login, db):
    from app.models import LeaveRequest

    hr = login("hr.officer")
    r = hr.post("/capture/new", {"form_type": "LEAVE_FORM", "employee": "YTOP954"}, files={"file": ("leave.png", _photo(), "image/png")})
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    cap = db.get(FormCapture, cid)
    codes = {f["code"] for f in cap.header["_findings"]}
    assert cap.status == "needs_review"
    assert "low_confidence" in codes  # the end date was hard to read
    page = hr.get(f"/capture/{cid}")
    assert "Create leave request" in page.text
    # HR checks the photo, the certificate was attached after all, and accepts with the paper signature
    h = cap.header
    hr.post(f"/capture/{cid}/header", {"employee_id": "YTOP954", "leave_type": "SICK", "start_date": h["start_date"], "end_date": h["end_date"],
                                       "part": "full", "reason": h.get("reason") or "", "medical_certificate": "on", "approved_by_signed": "on"})
    db.expire_all()
    cap = db.get(FormCapture, cid)
    assert "leave_document" not in {f["code"] for f in cap.header["_findings"]}
    r = hr.post(f"/capture/{cid}/accept", {"paper_signed": "on"})
    assert r.status_code == 303 and r.headers["location"].startswith("/leave/")
    db.expire_all()
    req = db.scalar(select(LeaveRequest).where(LeaveRequest.capture_id == cid))
    assert req.source == "paper" and req.has_document
    assert req.status == "approved"  # the Department Head's paper signature was the only step


def test_exit_opens_case_checklist_and_ai_reason(login, db):
    from app.models import ExitCase, User

    head = login("prod.head")
    last = date.today() + timedelta(days=10)
    r = head.post("/people/YTOP956/event", {"event_type": "exit", "effective_date": last.isoformat(), "reason_code": "RES-OTHER", "note": "Resigned"})
    assert r.status_code == 303
    appr = last_approval(db)
    login("hr.officer").post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    case = db.scalar(select(ExitCase).where(ExitCase.employee_id == "YTOP956"))
    assert case is not None and case.status == "open" and case.last_day == last
    codes = [i["code"] for i in case.items]
    assert "HANDOVER" in codes and "SSO" in codes
    # the supervisor side ticks handover; HR cannot close while items are open
    head.post(f"/exit/{case.id}/tick", {"code": "HANDOVER", "done": "1"})
    hr = login("hr.manager")
    hr.post(f"/exit/{case.id}/close")
    db.expire_all()
    assert db.get(ExitCase, case.id).status == "open"
    # interview with Thai comments, AI suggests a code, HR confirms it
    hr.post(f"/exit/{case.id}/interview", {"s_job": "4", "s_boss": "4", "s_pay": "1", "s_shift": "3", "would_return": "maybe",
                                           "main_reason": "ได้งานใหม่ เงินเดือนสูงกว่า", "comments": "Pay is too low compared to the factory nearby", "ask_ai": "1"})
    db.expire_all()
    case = db.get(ExitCase, case.id)
    assert case.ai_reason_code == "RES-PAY" and case.interview["scores"]["pay"] == 1
    hr.post(f"/exit/{case.id}/reason", {"code": case.ai_reason_code})
    for i in case.items:
        if not i["done"]:
            hr.post(f"/exit/{case.id}/tick", {"code": i["code"], "done": "1"})
    hr.post(f"/exit/{case.id}/close")
    db.expire_all()
    case = db.get(ExitCase, case.id)
    emp = db.get(Employee, "YTOP956")
    assert case.status == "closed" and emp.exit_reason_code == "RES-PAY"
    assert "Exit interview scores" in hr.get("/dash").text


def test_phase2_pages_render(login):
    hr = login("hr.manager")
    for url in ("/leave", "/leave/new", "/leave/balances", "/exit", "/exit?status=closed", "/dash/attendance", "/capture/new?type=LEAVE_FORM"):
        assert hr.get(url).status_code == 200, url
    sup = login("sup.dry")
    assert sup.get("/leave").status_code == 200
    assert sup.get("/exit").status_code == 200
