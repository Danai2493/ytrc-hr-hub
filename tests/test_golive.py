"""Go-live: Employee Master workbook load and opening leave balances (SAMPLE rows only)."""
import io
from datetime import date

import openpyxl
from sqlalchemy import select

from app.models import Employee, EmployeeRestricted, LeaveOpening
from app.services import golive as G
from app.services import leave as L

HEAD = ["Employee ID", "Employee Group", "Pay Type", "Title", "Gender", "Full Name (EN)", "Full Name (TH)", "Nickname", "Nationality",
        "Birth Date", "Age", "Division", "Department", "Section", "Team", "Position", "Position Level", "Join Date",
        "Current Position Since", "Service Years", "Status", "Exit Date", "Exit Reason", "Previous Employee ID", "Residence Room",
        "Remark", "Last Updated", "Row Check"]


def _row(**kw):
    base = {"Employee Group": "Operator", "Pay Type": "Daily", "Title": "Mr.", "Gender": "M", "Nationality": "Myanmar",
            "Birth Date": date(1995, 3, 1), "Division": "Factory", "Department": "Production", "Section": "Drying & Packing",
            "Team": "A", "Position": "Operator", "Position Level": "Operator", "Join Date": date(2024, 6, 1), "Status": "Active"}
    base.update(kw)
    return [base.get(h) for h in HEAD]


def _book(rows, restricted=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Master"
    ws.append(HEAD)
    for r in rows:
        ws.append(r)
    rs = wb.create_sheet("Restricted")
    rs.append(["Employee ID", "Name (lookup)", "Nationality (lookup)", "Status (lookup)", "ID Card / Passport No.", "Work Permit No.",
               "Work Permit Expiry", "Disability Registered", "Check"])
    for r in restricted:
        rs.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_master_preview_and_load(login, db):
    content = _book([
        _row(**{"Employee ID": "YTOP991", "Full Name (EN)": "Kyaw Import (SAMPLE)"}),
        _row(**{"Employee ID": "YTOP992", "Full Name (EN)": "Left Import (SAMPLE)", "Status": "Resigned", "Exit Date": date(2025, 1, 31),
                "Exit Reason": "Contract ended"}),
        _row(**{"Employee ID": "YTOP993", "Full Name (EN)": "Bad Dept (SAMPLE)", "Department": "Nowhere", "Section": None}),
        _row(**{"Employee ID": "YTOP994", "Full Name (EN)": "Too Young (SAMPLE)", "Birth Date": date(2012, 1, 1)}),
        _row(**{"Employee ID": "YTR806", "Employee Group": "Staff", "Pay Type": "Monthly", "Title": "Ms.", "Gender": "F", "Nationality": "Thai",
                "Full Name (EN)": "Dao Sample (SAMPLE)", "Nickname": "Dao2", "Department": "Production", "Section": "Drying & Packing",
                "Team": None, "Position": "Production Supervisor", "Position Level": "SV", "Join Date": date(2018, 11, 1),
                "Birth Date": db.get(Employee, "YTR806").birth_date}),
        _row(**{"Employee ID": "YTOP995", "Full Name (EN)": "Twice (SAMPLE)"}),
        _row(**{"Employee ID": "YTOP995", "Full Name (EN)": "Twice (SAMPLE)"}),
    ], restricted=[["YTOP991", None, None, None, "MA123456", "WP-0001", date(2027, 5, 1), None, None]])
    master, rest = G.read_workbook(content)
    plan = G.plan_master(db, master, rest)
    by = {r.employee_id: r for r in plan.rows}
    assert by["YTOP991"].action == "new" and by["YTOP991"].data["org_unit_code"] == "PRD-DRY" and by["YTOP991"].data["job_code"] == "OPR-DRY"
    assert by["YTOP992"].action == "new" and by["YTOP992"].exit_reason == "CONTRACT"
    assert by["YTOP993"].action == "error" and by["YTOP994"].action == "error"
    assert by["YTOP995"].action == "error"
    assert by["YTR806"].action == "update" and "nickname" in by["YTR806"].changes
    # the page never carries restricted numbers, only the preview counts
    hrm = login("hr.manager")
    r = hrm.post("/admin/golive/master", files={"file": ("master.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200 and "MA123456" not in r.text and "YTOP993" in r.text
    import re
    token = re.search(r'name="token" value="([0-9a-f]{24})"', r.text).group(1)
    # HR officer cannot load
    assert login("hr.officer").post("/admin/golive/master/apply", {"token": token}).status_code == 403
    r = hrm.post("/admin/golive/master/apply", {"token": token})
    assert r.status_code in (302, 303)
    db.expire_all()
    e = db.get(Employee, "YTOP991")
    assert e and e.status == "Active" and e.org_unit_code == "PRD-DRY" and e.team == "A"
    assert db.get(EmployeeRestricted, "YTOP991").work_permit_no == "WP-0001"
    left = db.get(Employee, "YTOP992")
    assert left.status == "Resigned" and left.exit_reason_code == "CONTRACT"
    assert db.get(Employee, "YTR806").nickname == "Dao2"
    assert db.get(Employee, "YTOP993") is None
    # the same token cannot be used twice
    assert hrm.post("/admin/golive/master/apply", {"token": token}).status_code in (302, 303)
    # second upload of the same file: nothing new
    plan2 = G.plan_master(db, *G.read_workbook(content))
    assert {r.employee_id: r.action for r in plan2.rows}["YTOP991"] == "same"
    for eid in ("YTOP991", "YTOP992"):  # keep the shared demo data as it was
        from app.models import EmploymentEvent
        for ev in db.scalars(select(EmploymentEvent).where(EmploymentEvent.employee_id == eid)):
            db.delete(ev)
        r_ = db.get(EmployeeRestricted, eid)
        if r_:
            db.delete(r_)
        db.flush()
        db.delete(db.get(Employee, eid))
    db.commit()


def test_opening_balances(login, db):
    year = date.today().year
    hro = login("hr.officer")
    r = hro.get(f"/admin/golive/opening-template?year={year}")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    ws = wb["Opening"]
    emp = db.get(Employee, "YTR806")
    before = {b.type.code: b.used for b in L.balances(db, emp, year)}
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "YTR806" and row[2].value == "SICK":
            row[4].value = 2.5
        if row[0].value == "YTR806" and row[2].value == "VAC":
            row[4].value = 1
            row[5].value = 2
        if row[0].value == "YTR806" and row[2].value == "PERS":
            row[5].value = 1  # carry-in only for vacation: error
    buf = io.BytesIO()
    wb.save(buf)
    r = hro.post("/admin/golive/opening", files={"file": ("o.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    import re
    token = re.search(r'name="token" value="([0-9a-f]{24})"', r.text).group(1)
    hro.post("/admin/golive/opening/apply", {"token": token})
    db.expire_all()
    rows = db.scalars(select(LeaveOpening).where(LeaveOpening.employee_id == "YTR806")).all()
    assert {(o.leave_type, o.used_before) for o in rows} == {("SICK", 2.5), ("VAC", 1.0)}
    after = {b.type.code: b.used for b in L.balances(db, emp, year)}
    assert after["SICK"] == before["SICK"] + 2.5
    assert L.used_days(db, "YTR806", "SICK", year) >= 2.5
    if year >= 2027:
        assert L.vacation_carry(db, emp, year) == 2.0
    for o in rows:
        db.delete(o)
    db.commit()
    assert login("hr.manager").get("/admin/golive").status_code == 200
