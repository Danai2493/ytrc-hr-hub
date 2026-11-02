"""Plan ahead: succession for critical positions, leaving-risk flags (HR only), workforce plan."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import Employee, Position, StayTalk, SuccessionPlan, Successor
from app.services import plan as P


def test_pages_and_who_sees_what(login):
    hrm, gm, head, sup = login("hr.manager"), login("gm"), login("prod.head"), login("sup.dry")
    for url in ("/plan", "/plan/position/POS-0006", "/plan/risk", "/plan/risk/YTOP964", "/plan/workforce", "/plan/workforce?months=24&turnover=20&growth=5&dept=PRD"):
        r = hrm.get(url)
        assert r.status_code == 200, url
    assert "SAMPLE" in hrm.get("/plan/risk/YTOP964").text  # stay talk shows
    assert gm.get("/plan/risk").status_code == 200
    # department head: succession and headcount for their area, never the leaving-risk page
    assert head.get("/plan").status_code == 200 and head.get("/plan/workforce").status_code == 200
    assert head.get("/plan/risk").status_code == 403
    assert "/plan/risk" not in head.get("/plan").text
    # the production head does not see their own position's succession plan
    assert head.get("/plan/position/POS-0005").status_code == 404
    assert "POS-0005" not in head.get("/plan").text
    assert sup.get("/plan").status_code == 403


def test_add_and_remove_successor(login, db):
    hro = login("hr.officer")
    url = "/plan/position/POS-0009"
    hro.post(url, {"do": "add", "employee_id": "YTR815", "readiness": "1_2y", "gaps": "Budgeting (SAMPLE)"})
    hro.post(url, {"do": "add", "employee_id": "YTR809", "readiness": "now"})  # the holder: refused
    hro.post(url, {"do": "add", "employee_id": "YTR815", "readiness": "now"})  # twice: refused
    db.expire_all()
    plan = db.scalar(select(SuccessionPlan).where(SuccessionPlan.position_id == "POS-0009"))
    assert [(s.employee_id, s.readiness) for s in plan.successors] == [("YTR815", "1_2y")]
    flags = {f["code"] for f in P.position_flags(db, db.get(Position, "POS-0009"), plan)}
    assert {"sp_none_ready", "sp_single"} <= flags and "sp_none" not in flags
    hro.post(url, {"do": "remove", "sid": str(plan.successors[0].id)})
    db.expire_all()
    assert not db.scalars(select(Successor).where(Successor.plan_id == plan.id)).all()
    assert "sp_none" in {f["code"] for f in P.position_flags(db, db.get(Position, "POS-0009"), plan)}


def test_retirement_flag(db):
    pos = db.get(Position, "POS-0015")  # demo technician reaches retirement age next year
    codes = {f["code"] for f in P.position_flags(db, pos, P.plan_for(db, pos))}
    assert "sp_retire" in codes


def test_risk_signals_explain_and_stay_private(login, db):
    hrm = login("hr.manager")
    from app.models import User
    u = db.scalar(select(User).where(User.username == "hr.manager"))
    rows = {r.employee.employee_id: r for r in P.risks(db, u, min_level="all")}
    assert u.employee_id not in rows  # never your own score
    r = rows["YTOP983"]  # fixed-term contract ending within 90 days in the demo
    assert any(s["code"] == "rk_contract_end" for s in r.signals)
    for r in rows.values():
        assert r.score == sum(s["weight"] for s in r.signals)
        assert r.level == ("high" if r.score >= 6 else "watch" if r.score >= 4 else "low")
    # a stay talk needs real notes; HR only
    hrm.post("/plan/risk/YTOP983", {"on": date.today().isoformat(), "note": "ok"})
    db.expire_all()
    assert not db.scalars(select(StayTalk).where(StayTalk.employee_id == "YTOP983")).all()
    hrm.post("/plan/risk/YTOP983", {"on": date.today().isoformat(), "note": "SAMPLE. Wants a permanent contract; HR to check the line's budget.",
                                    "follow_up_on": (date.today() + timedelta(days=30)).isoformat()})
    db.expire_all()
    assert len(db.scalars(select(StayTalk).where(StayTalk.employee_id == "YTOP983")).all()) == 1
    login("prod.head").post("/plan/risk/YTOP983", {"note": "Should not be saved (SAMPLE)"})
    db.expire_all()
    assert len(db.scalars(select(StayTalk).where(StayTalk.employee_id == "YTOP983")).all()) == 1


def test_forecast_adds_up(db):
    from app.models import User
    u = db.scalar(select(User).where(User.username == "hr.manager"))
    f = P.forecast(db, u, months=12, turnover=0)
    active = db.scalars(select(Employee).where(Employee.status == "Active")).all()
    assert f.total["start"] == len(active)
    # with no resignations assumed, the last month = today - known leavers + planned hires
    assert round(f.total["projected"][-1]) == f.total["start"] - sum(f.total["known"]) + sum(f.total["hires"])
    assert sum(f.total["known"]) >= 2  # two fixed-term contracts end inside the year
    f2 = P.forecast(db, u, months=24, turnover=24)
    assert len(f2.months) == 24 and f2.total["projected"][-1] < f.total["projected"][-1]
    for d in f.rows:
        s = P.start_hiring(f, d)
        assert s is None or s[1] >= 1
