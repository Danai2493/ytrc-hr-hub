"""Appraisal and KPI: goals, AI help, reviews, operator facts, calibration, release, export, scope."""
from sqlalchemy import select

from app.models import Appraisal, DevelopmentPlan, Employee, PerformanceCycle
from app.services import perform as P


def _cycle(db, name):
    return db.scalar(select(PerformanceCycle).where(PerformanceCycle.name == name))


def _goal_form(items, do="agree"):
    f = {"do": do}
    for n, i in enumerate(items):
        f.update({f"id{n}": i["id"], f"t{n}": i["title"], f"k{n}": i.get("kpi_code") or "", f"g{n}": i.get("goal_id") or "",
                  f"m{n}": i.get("measure") or "", f"u{n}": i.get("unit") or "", f"d{n}": i.get("direction") or "up",
                  f"tg{n}": i.get("target"), f"w{n}": i.get("weight")})
    return f


def test_scores_from_the_numbers():
    assert P.score_from(P.achievement("up", 100, 125)) == 5
    assert P.score_from(P.achievement("up", 100, 96)) == 3
    assert P.score_from(P.achievement("down", 10, 12)) in (2, 3)
    assert P.score_from(P.achievement("down", 0, 0)) == 5  # zero injuries hit
    assert P.score_from(P.achievement("up", 100, 50)) == 1
    assert P.achievement("up", 100, None) is None


def test_goal_checks():
    good = [{"title": f"K{n}", "target": 10, "weight": w, "goal_id": 1 if n == 0 else None} for n, w in enumerate((40, 30, 30))]
    assert not [f for f in P.item_checks(good) if f["level"] == "block"]
    bad = [dict(good[0], weight=50), dict(good[1], target=None)]
    codes = {f["code"] for f in P.item_checks(bad)}
    assert {"kpi_weights", "kpi_no_target", "kpi_count", "kpi_weight_range"} <= codes
    assert P.item_checks([])[0]["code"] == "kpi_none"


def test_year_from_goals_to_acknowledgement(login, db):
    hrm, emp, other = login("hr.manager"), login("hr.officer"), login("hr.officer2")
    r = hrm.post("/perform/cycles", {"name": "2031 performance year", "start": "2031-01-01", "end": "2031-12-31", "goals_due": "2031-02-15",
                                     "midyear": "2031-07-15", "review_due": "2032-01-20", "groups": ["Staff"]})
    assert r.status_code == 303
    c = _cycle(db, "2031 performance year")
    a = db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.employee_id == "YTR802"))
    assert a.form == "staff" and a.reviewer_employee_id == "YTR801" and a.status == "goals"
    assert not db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.employee_id == "YTOP951"))  # Staff group only
    url = f"/perform/a/{a.id}"
    assert emp.get(url).status_code == 200
    assert other.get(url).status_code == 200  # HR can open, but
    emp.post(url, {"do": "ai_suggest"})
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.ai_used and 3 <= len(a.items) <= 7 and sum(i["weight"] for i in a.items) == 100
    emp.post(url, _goal_form(a.items))  # cannot agree own goals
    db.expire_all()
    assert db.get(Appraisal, a.id).status == "goals"
    broken = [dict(i) for i in a.items]
    broken[0]["weight"] = 1
    hrm.post(url, _goal_form(broken))  # weights do not add up
    db.expire_all()
    assert db.get(Appraisal, a.id).status == "goals"
    hrm.post(url, _goal_form(a.items))
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.status == "goals_agreed" and a.goals_agreed_at
    hrm.post(url, _goal_form(a.items, "goals"))  # change after agreement needs a reason
    hrm.post(f"/perform/cycles/{c.id}/status", {"status": "review"})
    self_form = {"do": "self_send", "self_note": "Good year"}
    for i in a.items:
        self_form[f"a_{i['id']}"] = str(i["target"] * 1.25 if i["direction"] != "down" else i["target"] * 0.5)
        self_form[f"s_{i['id']}"] = "4"
    emp.post(url, self_form)
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.status == "manager" and a.self_at
    assert "Supervisor summary" not in emp.get(url).text
    mgr = {"do": "mgr_send", "mgr_note": "Strong year"}
    mgr.update({f"cm_{k}": "4" for k in a.competencies})
    hrm.post(url, mgr)
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert all(i["mgr"] == i["auto"] == 5 for i in a.items)
    assert a.kpi_score == 5 and a.comp_score == 4 and a.overall == 4.7 and a.proposed_rating == 5
    if a.status == "endorse":
        hrm.post(url, {"do": "endorse", "rating": "5"})
        db.expire_all()
    assert db.get(Appraisal, a.id).status == "calibration"
    assert "A" not in emp.get(url).text.split("big-rating")[0][-5:]  # nothing released yet
    hrm.post(f"/perform/cycles/{c.id}/status", {"status": "calibration"})
    hrm.post(url, {"do": "calibrate", "rating": "2", "note": ""})  # change needs a reason
    db.expire_all()
    assert db.get(Appraisal, a.id).final_rating is None
    hrm.post(url, {"do": "calibrate", "rating": "2", "note": "SAMPLE: test of the D path"})
    hrm.post(f"/perform/cycles/{c.id}/status", {"status": "closed"})  # others not finished
    db.expire_all()
    assert db.get(PerformanceCycle, c.id).status == "calibration"
    for x in db.scalars(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.id != a.id)):
        x.status, x.proposed_rating = "calibration", 3
    db.commit()
    hrm.post(f"/perform/cycles/{c.id}/status", {"status": "closed"})
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.status == "released" and a.final_rating == 2 and a.plan_id
    plan = db.get(DevelopmentPlan, a.plan_id)
    assert plan.employee_id == "YTR802" and "improvement" in plan.title.lower()
    page = emp.get(url).text
    assert "big-rating" in page and ">D<" in page
    emp.post(url, {"do": "ack", "agree": "0", "ack_note": "I want to talk about it"})
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.ack_at and a.ack_agree is False
    csv = hrm.get(f"/perform/cycles/{c.id}/export.csv").text
    assert "salary" not in csv.lower() and "merit_guideline_pct" in csv
    assert any(line.startswith("YTR802,") and ",2,D," in line for line in csv.splitlines())
    assert emp.get(f"/perform/cycles/{c.id}/export.csv").status_code == 403


def test_operator_form_uses_the_facts(login, db):
    c = _cycle(db, f"{__import__('datetime').date.today().year} performance year")
    done = db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.form == "operator", Appraisal.status == "calibration"))
    assert done.facts["planned_days"] and done.facts["attendance_pct"] is not None
    assert all(p.get("score") for p in done.parts.values())
    a = db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.form == "operator", Appraisal.status == "goals_agreed",
                                          Appraisal.reviewer_employee_id == "YTR806"))
    sup = login("sup.dry")
    url = f"/perform/a/{a.id}"
    sup.post(url, {"do": "skip_self", "reason": "No login"})
    db.expire_all()
    a = db.get(Appraisal, a.id)
    assert a.status == "manager"
    P.collect_facts(db, a)
    sug = P.suggest_parts(db, a)
    db.rollback()
    form = {"do": "mgr_send"}
    form.update({f"cm_{k}": "3" for k in a.competencies})
    form.update({f"p_{k}": str(v or 3) for k, v in sug.items() if k != "behaviour"})
    form["p_attendance"] = str(5 if sug["attendance"] != 5 else 1)  # differs from the facts, no note
    sup.post(url, form)
    db.expire_all()
    assert db.get(Appraisal, a.id).status == "manager"
    form["pn_attendance"] = "Covered two extra shifts (SAMPLE)"
    sup.post(url, form)
    db.expire_all()
    assert db.get(Appraisal, a.id).status in ("endorse", "calibration")


def test_scope(login, db):
    c = _cycle(db, f"{__import__('datetime').date.today().year} performance year")
    a = db.scalar(select(Appraisal).join(Employee).where(Appraisal.cycle_id == c.id, Employee.org_unit_code == "PRD-DRY", Appraisal.form == "operator"))
    assert login("sup.pcl").get(f"/perform/a/{a.id}").status_code == 404
    assert login("sup.dry").get(f"/perform/a/{a.id}").status_code == 200
    own = db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.employee_id == "YTR806"))
    page = login("sup.dry").get(f"/perform/a/{own.id}").text
    assert "Send review" not in page and "proposed" not in page


def test_health_and_pages(login, db):
    from app.models import DataIssue
    from app.services import health

    health.run(db)
    db.commit()
    assert db.scalar(select(DataIssue).where(DataIssue.rule_code == "GOALS_LATE", DataIssue.status == "open"))
    hrm = login("hr.manager")
    old = _cycle(db, f"{__import__('datetime').date.today().year - 1} performance year")
    cur = _cycle(db, f"{__import__('datetime').date.today().year} performance year")
    for url in ("/perform", f"/perform?cycle={old.id}", f"/perform/cycles/{cur.id}/goals", "/perform/kpis",
                f"/perform/cycles/{cur.id}/calibration", f"/perform/cycles/{old.id}/calibration?unit=PRD-DRY"):
        assert hrm.get(url).status_code == 200, url
    for a in db.scalars(select(Appraisal).where(Appraisal.cycle_id.in_((old.id, cur.id)))).all()[::7]:
        assert hrm.get(f"/perform/a/{a.id}").status_code == 200, a.id
    assert hrm.get(f"/perform/cycles/{old.id}/export.csv").status_code == 200
    assert hrm.get(f"/perform/cycles/{cur.id}/export.csv").status_code == 400
    assert "Performance" in login("prod.head").get("/").text
