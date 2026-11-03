"""Personnel documents (HR-only scans, expiry) and the policy library with acknowledgement."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import EmployeeDocument, EmployeeRestricted, Policy, PolicyAck, User
from app.services import docs as D
from app.services import health


def test_pages_and_access(login):
    hrm, emp, head = login("hr.manager"), login("sup.dry"), login("prod.head")
    for url in ("/docs", "/docs/watch", "/people/YTOP951", "/docs/policy/new"):
        assert hrm.get(url).status_code == 200, url
    p = hrm.get("/docs").text
    assert "POL-HR-03" in p and "Privacy" in p
    assert emp.get("/docs").status_code == 200 and emp.get("/docs/watch").status_code == 403
    # the privacy notice is waiting on the supervisor's home page
    assert "Policies to read and acknowledge" in emp.get("/").text
    # supervisors never see a team member's documents; the employee sees their own list
    assert 'id="docs"' not in head.get("/people/YTR806").text
    assert 'id="docs"' in emp.get("/people/YTR806").text


def test_add_document_scan_hr_only(login, db):
    hro = login("hr.officer")
    pdf = b"%PDF-1.4\n%SAMPLE\n"
    exp = date.today() + timedelta(days=400)
    r = hro.c.post("/people/YTOP953/docs", data={"_csrf": hro.csrf, "doc_type": "work_permit", "number": "WP 1234-5678", "expires_on": exp.isoformat()},
                   files={"file": ("wp.pdf", pdf, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    d = db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == "YTOP953", EmployeeDocument.doc_type == "work_permit",
                                                   EmployeeDocument.current.is_(True))).one()
    assert d.number_tail == "5678" and d.file_path  # only the last 4 characters are kept
    assert db.get(EmployeeRestricted, "YTOP953").work_permit_expiry == exp  # enter once: drives the legal check
    assert hro.get(f"/people/docs/{d.id}/file").status_code == 200
    assert login("prod.head").get(f"/people/docs/{d.id}/file").status_code == 403
    # an expiring type without a date is refused
    hro.post("/people/YTOP953/docs", {"doc_type": "passport", "paper_only": "1"})
    db.expire_all()
    assert not db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == "YTOP953", EmployeeDocument.doc_type == "passport",
                                                          EmployeeDocument.uploaded_by.like("%Fon%"))).all()


def test_watchlist_and_health(db):
    rows = D.watchlist(db)
    kinds = {r.problem for r in rows}
    assert {"expired", "soon", "missing"} <= kinds
    health.run(db)
    db.flush()
    from app.models import DataIssue
    codes = {i.rule_code for i in db.scalars(select(DataIssue).where(DataIssue.status == "open"))}
    assert "DOC_EXPIRY" in codes and "DOC_MISSING" in codes
    db.rollback()


def test_policy_version_publish_ack(login, db):
    hro, hrm, emp = login("hr.officer"), login("hr.manager"), login("sup.dry")
    cur = db.scalar(select(Policy).where(Policy.code == "POL-HR-02", Policy.status == "published"))
    hro.post(f"/docs/policy/{cur.id}", {"do": "version"})
    db.expire_all()
    new = db.scalar(select(Policy).where(Policy.code == "POL-HR-02", Policy.status == "draft"))
    assert new.version == cur.version + 1
    hro.post(f"/docs/policy/{new.id}", {"do": "publish"})  # an HR officer cannot publish
    db.expire_all()
    assert db.get(Policy, new.id).status == "draft"
    hro.c.post(f"/docs/policy/{new.id}", data={"_csrf": hro.csrf, "do": "save", "code": "POL-HR-02", "title_en": new.title_en, "applies_to": "all",
                                               "summary": "SAMPLE. New OT limit wording.", "changes": "SAMPLE change", "ack_required": "1", "ack_days": "7"})
    hrm.post(f"/docs/policy/{new.id}", {"do": "publish"})
    db.expire_all()
    assert db.get(Policy, new.id).status == "published" and db.get(Policy, cur.id).status == "retired"
    # everyone acknowledges again; the supervisor does it in the app
    assert "Overtime" in emp.get("/").text
    emp.post(f"/docs/policy/{new.id}", {"do": "ack"})
    # HR records a paper briefing for operators
    hro.post(f"/docs/policy/{new.id}", {"do": "briefing", "method": "briefing", "on": date.today().isoformat(), "emp": ["YTOP951", "YTOP952"]})
    db.expire_all()
    acks = {a.employee_id: a.method for a in db.scalars(select(PolicyAck).where(PolicyAck.policy_id == new.id))}
    assert acks == {"YTR806": "web", "YTOP951": "briefing", "YTOP952": "briefing"}
    u = db.scalar(select(User).where(User.username == "sup.dry"))
    assert all(p.code != "POL-HR-02" for p in D.my_todo(db, u))
