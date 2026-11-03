"""Annual training plan, class cost and feedback, supervisor follow-up (did it work?)."""
from datetime import date

from sqlalchemy import select

from app.models import Course, TrainingPlanItem, TrainingRecord
from app.services import trainplan as TP


def test_pages(login):
    hrm, sup = login("hr.manager"), login("sup.dry")
    for url in ("/skills/plan", f"/skills/plan?year={date.today().year + 1}", "/skills/followup", "/skills/training"):
        assert hrm.get(url).status_code == 200, url
    assert sup.get("/skills/followup").status_code == 200
    assert "Did it work?" in sup.get("/skills").text


def test_plan_draft_and_edit(login, db):
    hro = login("hr.officer")
    y = date.today().year
    n0 = len(db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == y)).all())
    assert n0 > 0  # demo drafted this year's plan
    hro.post("/skills/plan", {"do": "draft", "year": str(y)})  # picks up needs found since
    db.expire_all()
    n1 = len(db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == y)).all())
    hro.post("/skills/plan", {"do": "draft", "year": str(y)})  # running it again adds nothing twice
    db.expire_all()
    assert n1 >= n0 and len(db.scalars(select(TrainingPlanItem).where(TrainingPlanItem.year == y)).all()) == n1
    c = db.scalar(select(Course).order_by(Course.code))
    hro.post("/skills/plan", {"year": str(y), "course": c.code, "month": "12", "people": "4", "budget": ""})
    db.expire_all()
    it = db.scalar(select(TrainingPlanItem).where(TrainingPlanItem.year == y, TrainingPlanItem.source == "manual", TrainingPlanItem.course_code == c.code))
    assert it.people == 4 and it.budget == (c.cost_per_person * 4 if c.cost_per_person else None)
    login("sup.dry").post("/skills/plan", {"year": str(y), "course": c.code, "month": "13", "people": "1"})  # not HR: 403
    rows, tot = TP.plan_vs_actual(db, y)
    assert tot["planned"] == sum(r.planned_people for r in rows)


def test_session_cost_and_followup(login, db):
    hro, sup = login("hr.officer"), login("sup.dry")
    r = db.scalar(select(TrainingRecord).where(TrainingRecord.session_ref.is_not(None)).order_by(TrainingRecord.on_date.desc()))
    ref = r.session_ref
    n = len(TP.session(db, ref))
    hro.post(f"/skills/training/session/{ref}", {"l1": "4.2", "cost": str(1000 * n)})
    db.expire_all()
    assert all(x.l1_score == 4.2 and x.cost == 1000 for x in TP.session(db, ref))
    hro.post(f"/skills/training/session/{ref}", {"l1": "7"})  # out of range: refused
    db.expire_all()
    assert TP.session(db, ref)[0].l1_score == 4.2
    from app.web.ot import team_for
    from app.models import User
    u = db.scalar(select(User).where(User.username == "sup.dry"))
    todo = TP.followups(db, {e.employee_id for e in team_for(db, u)})
    assert todo
    rec = todo[0]
    sup.post(f"/skills/followup/{rec.id}", {"result": "not_yet", "note": ""})  # needs a reason
    db.expire_all()
    assert db.get(TrainingRecord, rec.id).l3_result is None
    sup.post(f"/skills/followup/{rec.id}", {"result": "applied"})
    db.expire_all()
    assert db.get(TrainingRecord, rec.id).l3_result == "applied"
    eff = TP.effectiveness(db, date(2000, 1, 1), date.today())
    assert any(e.applied_pct is not None for e in eff) and any(e.l1_avg for e in eff)
