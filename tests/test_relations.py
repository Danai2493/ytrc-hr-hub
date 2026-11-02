"""Employee relations: confidentiality, the employee explains first, suspension and dismissal checks, who decides."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import ErCase


def _by(db, emp, status):
    return db.scalar(select(ErCase).where(ErCase.employee_id == emp, ErCase.status == status).order_by(ErCase.id.desc()))


def test_confidential_and_scope(login, db):
    harass = db.scalar(select(ErCase).where(ErCase.kind == "harassment"))
    griev = db.scalar(select(ErCase).where(ErCase.kind == "grievance"))
    disc = _by(db, "YTOP970", "open")
    hro, hrm, sup, head = login("hr.officer"), login("hr.manager"), login("sup.dry"), login("prod.head")
    assert hrm.get(f"/er/{harass.id}").status_code == 200
    assert hro.get(f"/er/{harass.id}").status_code == 404  # harassment: HR Manager and GM only
    assert hro.get(f"/er/{griev.id}").status_code == 200
    assert head.get(f"/er/{griev.id}").status_code == 404  # complaints stay with HR
    assert head.get(f"/er/{disc.id}").status_code == 200  # discipline in their units
    assert sup.get(f"/er/{disc.id}").status_code == 404  # PRD-PCL is not sup.dry's unit
    assert "Confidential" not in sup.get("/er").text


def test_open_and_checks(login, db):
    hro = login("hr.officer")
    t = date.today()
    r = hro.post("/er", {"kind": "discipline", "category": "conduct", "employee_id": "YTOP956", "incident_on": str(t - timedelta(days=2)),
                         "summary": "Argued loudly with the line leader and refused a task. (SAMPLE)"})
    assert r.status_code == 303 and r.headers["location"].startswith("/er/")
    c = _by(db, "YTOP956", "open")
    url = f"/er/{c.id}"
    hro.post(url, {"do": "propose", "outcome": "written", "reasons": "Refused a reasonable task, work rule 4.3 (SAMPLE)"})  # no statement yet
    db.expire_all()
    assert db.get(ErCase, c.id).status == "open"
    hro.post(url, {"do": "work", "statement": "I was tired after a double shift. (SAMPLE)", "findings": "Two witnesses. (SAMPLE)"})
    hro.post(url, {"do": "propose", "outcome": "suspension", "reasons": "Refused a reasonable task, work rule 4.3 (SAMPLE)",
                   "suspension_from": str(t), "suspension_days": "10"})  # over 7 days and no 50% pay
    db.expire_all()
    assert db.get(ErCase, c.id).status == "investigating"
    hro.post(url, {"do": "propose", "outcome": "written", "reasons": "Refused a reasonable task, work rule 4.3 (SAMPLE)"})
    db.expire_all()
    c = db.get(ErCase, c.id)
    assert c.status == "decision" and c.outcome == "written"
    hro.post(url, {"do": "agree"})  # the proposer never decides
    db.expire_all()
    assert db.get(ErCase, c.id).decided_at is None
    login("hr.manager").post(url, {"do": "agree"})
    db.expire_all()
    c = db.get(ErCase, c.id)
    assert c.decided_at and c.warning_until == c.incident_on + timedelta(days=365)
    assert login("hr.manager").get(f"{url}/letter").status_code == 200
    hro.post(url, {"do": "close", "letter_on": str(t), "signed": "no"})
    db.expire_all()
    assert db.get(ErCase, c.id).status == "closed" and db.get(ErCase, c.id).employee_signed is False


def test_dismissal_needs_gm_and_sees_the_warning(login, db):
    c = _by(db, "YTOP955", "investigating")  # repeat after a written warning
    url = f"/er/{c.id}"
    hro = login("hr.officer")
    hro.post(url, {"do": "propose", "outcome": "dismissal", "reasons": "Absent without leave again within a year of warning ER (SAMPLE)"})
    page = hro.get(url).text
    assert "s.119(4)" in page
    db.expire_all()
    assert db.get(ErCase, c.id).status == "decision"
    login("hr.manager").post(url, {"do": "agree"})  # dismissal is the GM's call
    db.expire_all()
    assert db.get(ErCase, c.id).decided_at is None
    login("gm").post(url, {"do": "send_back", "note": "Check the sick leave rules first (SAMPLE)"})
    db.expire_all()
    c = db.get(ErCase, c.id)
    assert c.status == "investigating" and c.outcome is None
