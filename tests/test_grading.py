"""Job grading: points, suggested grade, HR Manager agrees, reasons, checks, no pay."""
from sqlalchemy import inspect, select

from app.models import Grade, Job, JobEvaluation
from app.models.grading import FACTORS, points_of
from app.services import grading as G


def test_points_and_ladder(db):
    assert points_of({k: 1 for k in FACTORS}) == 100 and points_of({k: 5 for k in FACTORS}) == 500
    assert points_of({"know": 3}) is None
    assert G.suggest(db, 100) == "Gardener" and G.suggest(db, 500) == "MD" and G.suggest(db, 280) == "SV"
    assert not {c.key for c in inspect(Grade).columns} & {"salary", "min_salary", "max_salary", "pay"}


def test_rate_and_agree(login, db):
    j = db.scalar(select(Job).where(Job.code.not_in(select(JobEvaluation.job_code))).order_by(Job.code))
    url = f"/org/grades/job/{j.code}"
    hro, hrm = login("hr.officer"), login("hr.manager")
    assert hro.get(url).status_code == 200
    hro.post(url, {"do": "save", **{f"f_{k}": "3" for k in FACTORS}, "rationale": "Test (SAMPLE)"})
    db.expire_all()
    ev = db.scalar(select(JobEvaluation).where(JobEvaluation.job_code == j.code))
    assert ev.points == 300 and ev.suggested_grade == "SV" and ev.status == "draft"
    hro.post(url, {"do": "agree", "grade": "SV"})  # HR officer cannot agree
    db.expire_all()
    assert db.get(JobEvaluation, ev.id).status == "draft"
    hrm.post(url, {"do": "agree", "grade": "MG"})  # different from the points: needs a reason
    db.expire_all()
    assert db.get(JobEvaluation, ev.id).status == "draft"
    hrm.post(url, {"do": "agree", "grade": "MG", "reason": "Market and scope (SAMPLE)"})
    db.expire_all()
    ev = db.get(JobEvaluation, ev.id)
    assert ev.status == "agreed" and ev.agreed_grade == "MG" and db.get(Job, j.code).default_grade_code == "MG"
    assert "MG instead of SV" in ev.rationale
    hro.post(url, {"do": "save", **{f"f_{k}": "2" for k in FACTORS}})  # a change after agreement goes back to draft
    db.expire_all()
    assert db.get(JobEvaluation, ev.id).status == "draft"


def test_ai_and_page(login, db):
    j = db.scalar(select(Job).where(Job.code.not_in(select(JobEvaluation.job_code))).order_by(Job.code.desc()))
    login("hr.officer").post(f"/org/grades/job/{j.code}", {"do": "ai"})
    db.expire_all()
    ev = db.scalar(select(JobEvaluation).where(JobEvaluation.job_code == j.code))
    assert ev.ai_used and ev.points and ev.suggested_grade
    page = login("hr.manager").get("/org/grades").text
    assert "Grade ladder" in page and "jobs have no agreed grade yet" in page
    assert login("sup.dry").get("/org/grades").status_code == 403
