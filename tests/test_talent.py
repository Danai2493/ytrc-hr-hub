"""Talent review (9-box): mapping, scope, confidentiality, propose and agree."""
from datetime import date

from sqlalchemy import select

from app.models import Appraisal, PerformanceCycle, TalentReview, User
from app.models.talent import box_of, perf_level
from app.services import talent as T


def test_mapping():
    assert [perf_level(r) for r in (5, 4, 3, 2, 1, None)] == [3, 3, 2, 1, 1, None]
    assert box_of(3, 3)[0] == 9 and box_of(1, 1)[0] == 1 and box_of(2, 3)[0] == 6 and box_of(None, 2) is None
    assert T.potential_from({"learn": 3, "scope": 3, "lead": 2}) == 3
    assert T.potential_from({"learn": 2, "scope": 1, "lead": 1}) == 1
    assert T.potential_from({"learn": 2, "scope": None, "lead": 1}) is None


def _target(db, year):
    """An operator in Drying & Packing with a released rating and no review yet."""
    done = set(db.scalars(select(TalentReview.employee_id).where(TalentReview.year == year)))
    for a in db.scalars(select(Appraisal).join(PerformanceCycle, Appraisal.cycle_id == PerformanceCycle.id)
                        .where(Appraisal.status == "released", Appraisal.org_unit_code == "PRD-DRY", PerformanceCycle.released_at.is_not(None))):
        if a.employee_id not in done and a.cycle.period_end.year == year and a.employee.status == "Active" and a.employee.org_unit_code == "PRD-DRY" and a.employee_id != "YTR806":  # not sup.dry themself
            return a
    raise AssertionError("no free operator in the demo data")


def test_scope_and_confidential(login, db):
    year = date.today().year - 1  # the demo year with released results
    hrm, sup, emp = login("hr.manager"), login("sup.dry"), login("hr.officer")
    r = hrm.get(f"/talent?year={year}")
    assert r.status_code == 200 and "9-box grid" in r.text
    assert hrm.get(f"/talent/{year}/YTR801").status_code == 404  # never your own box
    assert sup.get(f"/talent?year={year}").status_code == 200
    other = db.scalar(select(Appraisal).where(Appraisal.status == "released", Appraisal.org_unit_code != "PRD-DRY", Appraisal.form == "staff"))
    assert sup.get(f"/talent/{year}/{other.employee_id}").status_code == 404  # outside the supervisor's unit
    assert emp.get(f"/talent/{year}/{other.employee_id}").status_code == 200  # HR sees every unit
    assert len(T.people(db, db.scalar(select(User).where(User.username == "sup.dry")), year)) < len(T.people(db, db.scalar(select(User).where(User.username == "hr.manager")), year))


def test_propose_and_agree(login, db):
    year = date.today().year - 1  # the demo year with released results
    a = _target(db, year)
    url = f"/talent/{year}/{a.employee_id}"
    sup, hrm = login("sup.dry"), login("hr.manager")
    assert sup.get(url).status_code == 200
    sup.post(url, {"do": "propose", "q_learn": "3", "q_scope": "3"})  # missing answers
    db.expire_all()
    t = db.scalar(select(TalentReview).where(TalentReview.year == year, TalentReview.employee_id == a.employee_id))
    assert t is None or t.status == "draft"
    sup.post(url, {"do": "propose", "q_learn": "3", "q_scope": "3", "q_lead": "2", "risk": "high", "next_move": "ready_2y"})  # high risk needs a reason
    db.expire_all()
    t = db.scalar(select(TalentReview).where(TalentReview.year == year, TalentReview.employee_id == a.employee_id))
    assert t is None or t.status == "draft"
    sup.post(url, {"do": "propose", "q_learn": "3", "q_scope": "3", "q_lead": "2", "risk": "high", "risk_reason": "Offer nearby", "next_move": "ready_2y"})
    db.expire_all()
    t = db.scalar(select(TalentReview).where(TalentReview.year == year, TalentReview.employee_id == a.employee_id))
    assert t.status == "proposed" and t.potential == 3 and t.box == box_of(perf_level(a.final_rating), 3)[0]
    sup.post(url, {"do": "agree"})  # supervisors do not agree
    db.expire_all()
    assert db.get(TalentReview, t.id).status == "proposed"
    hrm.post(url, {"do": "agree", "potential": "2"})  # change without a reason
    db.expire_all()
    assert db.get(TalentReview, t.id).status == "proposed"
    hrm.post(url, {"do": "agree", "potential": "2", "note": "Meeting: needs more time leading the shift"})
    db.expire_all()
    t = db.get(TalentReview, t.id)
    assert t.status == "agreed" and t.potential == 2 and t.box == box_of(perf_level(a.final_rating), 2)[0]
