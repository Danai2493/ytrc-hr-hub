"""Competency development: assessment rounds, reports, supervisor view, development plans."""
import re

from sqlalchemy import select

from app.models import Assessment, AssessmentRound, CompetencyQuestion, DevelopmentPlan, Employee, TrainingNeed


def _current(db):
    return db.scalar(select(AssessmentRound).where(AssessmentRound.status == "open").order_by(AssessmentRound.id.desc()))


def test_framework_is_our_own_and_complete(db):
    from app.seed.competency import COMPETENCIES, GROWTH_PATHS, QUESTIONS

    assert len(COMPETENCIES) == 20 and len(QUESTIONS) >= 60
    codes = {c[0] for c in COMPETENCIES}
    for c in codes:
        assert sum(1 for q in QUESTIONS if q[0] == c) >= 3, c  # every competency is measured by 3+ questions
    assert all(q[3] for q in QUESTIONS)  # every question has Thai text
    assert all(set(p[-1]) <= codes for p in GROWTH_PATHS)


def test_reverse_questions_are_scored_the_other_way(db):
    from app.services import growth

    qs = [q for q in db.scalars(select(CompetencyQuestion).where(CompetencyQuestion.competency_code == "SE2"))]
    rev = next(q for q in qs if q.reverse)
    fwd = next(q for q in qs if not q.reverse)
    s = growth.score(db, {str(fwd.id): 5, str(rev.id): 1})
    assert s["SE2"] == 5.0


def test_operators_get_the_short_set(db):
    from app.services import growth

    core = growth.questions_for(db, "core")
    leader = growth.questions_for(db, "leader")
    assert len(core) < len(growth.questions_for(db, "staff")) < len(leader)
    assert not any(q.competency.leaders_only for q in core)


def test_hr_enters_paper_form_and_report_is_not_a_rating(login, db):
    hr = login("hr.officer")
    a = db.scalar(select(Assessment).where(Assessment.round_id == _current(db).id, Assessment.status == "open", Assessment.audience == "core"))
    r = hr.get(f"/growth/a/{a.id}")
    assert r.status_code == 200 and "paper form" in r.text
    qids = [int(x) for x in re.findall(r'name="q_(\d+)"', r.text)]
    qids = sorted(set(qids))
    half = {f"q_{q}": "4" for q in qids[: len(qids) // 2]}
    r = hr.post(f"/growth/a/{a.id}", {**half, "submit": "1"})  # not finished: answers kept, not sent
    db.expire_all()
    a = db.get(Assessment, a.id)
    assert a.status == "open" and len(a.answers) == len(half)
    rest = {f"q_{q}": str(1 + i % 5) for i, q in enumerate(qids)}
    assert hr.post(f"/growth/a/{a.id}", {**rest, "submit": "1"}).headers["location"].endswith("/report")
    db.expire_all()
    a = db.get(Assessment, a.id)
    assert a.status == "submitted" and a.source == "paper" and a.scores
    r = hr.get(f"/growth/a/{a.id}/report")
    assert r.status_code == 200 and "Strengths to build on" in r.text and "<svg" in r.text
    low = r.text.lower()
    for word in ("good employee", "bad employee", "rank", "grade a"):
        assert word not in low


def test_supervisor_view_plan_and_training_need(login, db):
    sup = login("sup.dry")
    a = db.scalar(select(Assessment).join(Employee).where(Assessment.round_id == _current(db).id, Assessment.status == "submitted",
                                                          Employee.org_unit_code == "PRD-DRY", Assessment.employee_id != "YTOP951",
                                                          Assessment.employee_id.not_in(("YTOP952", "YTR806"))))
    codes = list(a.scores)
    r = sup.post(f"/growth/a/{a.id}/manager", {f"m_{codes[0]}": "5", f"m_{codes[1]}": "9", "note": "Strong on handover"})
    assert r.status_code == 303
    db.expire_all()
    a = db.get(Assessment, a.id)
    assert a.manager_scores == {codes[0]: 5}  # out-of-scale value ignored
    r = sup.post(f"/growth/a/{a.id}/plan")
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    p = db.get(DevelopmentPlan, pid)
    focus = [it["competency"] for it in p.items]
    assert len(focus) == 2
    r = sup.post(f"/growth/plan/{pid}", {"do": "suggest", "c0": focus[0], "c1": focus[1]})
    db.expire_all()
    p = db.get(DevelopmentPlan, pid)
    assert p.ai_used and "AI SAMPLE" in p.items[0]["actions"][0]["text"]
    form = {"do": "agree", "c0": focus[0], "g0": "Lead the shift handover", "p0": "10",
            "t0_0": "course", "a0_0": "Leadership class", "k0_0": "CR-LDR-01", "d0_0": "2030-01-31", "review_on": "2030-03-01"}
    sup.post(f"/growth/plan/{pid}", form)
    db.expire_all()
    p = db.get(DevelopmentPlan, pid)
    assert p.status == "agreed" and p.agreed_by
    need = db.scalar(select(TrainingNeed).where(TrainingNeed.employee_id == a.employee_id, TrainingNeed.course_code == "CR-LDR-01"))
    assert need and need.reason == "idp"


def test_other_supervisor_cannot_see(login, db):
    sup = login("sup.pcl")
    a = db.scalar(select(Assessment).join(Employee).where(Assessment.status == "submitted", Employee.org_unit_code == "PRD-DRY"))
    assert sup.get(f"/growth/a/{a.id}/report").status_code == 404


def test_small_groups_are_hidden(db):
    from app.services import growth

    s = growth.round_summary(db, _current(db))
    assert s["units"] and all(u["hidden"] == (u["n"] < growth.MIN_GROUP) for u in s["units"].values())
    assert all("clusters" not in u for u in s["units"].values() if u["hidden"])


def test_pages_render(login, db):
    hr = login("hr.officer")
    rid = _current(db).id
    for url in ("/growth", f"/growth/rounds/{rid}", "/growth/framework", "/growth/framework?print=1&audience=leader"):
        r = hr.get(url)
        assert r.status_code == 200, url
    assert "Growth" in login("sup.dry").get("/").text
    p = db.scalar(select(DevelopmentPlan).where(DevelopmentPlan.status == "agreed"))
    assert hr.get(f"/growth/plan/{p.id}").status_code == 200


def test_open_round_invites_groups(login, db):
    hr = login("hr.officer")
    r = hr.post("/growth/rounds", {"name": "Test round", "opens_on": "2030-01-01", "closes_on": "2030-01-21", "groups": ["Staff"]})
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    rows = db.scalars(select(Assessment).where(Assessment.round_id == rid)).all()
    assert rows and all(a.employee.employee_group == "Staff" for a in rows)
    assert hr.post("/growth/rounds", {"name": "Bad", "opens_on": "2030-01-21", "closes_on": "2030-01-01", "groups": ["Staff"]}).headers["location"] == "/growth"
