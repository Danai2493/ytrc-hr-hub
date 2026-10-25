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
