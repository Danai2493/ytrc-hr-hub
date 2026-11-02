"""Pulse survey: answer once, anonymous storage, small groups hidden, comments HR only."""
from datetime import date, timedelta

from sqlalchemy import inspect, select

from app.models import PulseInvite, PulseResponse, PulseSurvey, User
from app.models.pulse import MIN_GROUP
from app.services import pulse as P


def _open(db):
    return db.scalar(select(PulseSurvey).where(PulseSurvey.status == "open").order_by(PulseSurvey.id.desc()))


def test_response_has_no_person():
    cols = {c.key for c in inspect(PulseResponse).columns}
    assert not cols & {"employee_id", "user_id", "created_at", "submitted_at"}


def test_answer_once_and_home_banner(login, db):
    s = _open(db)
    c = login("hr.officer")
    assert f"/pulse/{s.id}/answer" in c.get("/").text
    before = db.query(PulseResponse).filter_by(survey_id=s.id).count()
    form = {f"q_{q['id']}": "4" for q in s.questions}
    form["comment"] = "More shade at the bus stop (SAMPLE)"
    c.post(f"/pulse/{s.id}/answer", form)
    c.post(f"/pulse/{s.id}/answer", form)  # second time is refused
    db.expire_all()
    assert db.query(PulseResponse).filter_by(survey_id=s.id).count() == before + 1
    assert db.scalar(select(PulseInvite).where(PulseInvite.survey_id == s.id, PulseInvite.employee_id == "YTR802")).done
    assert f"/pulse/{s.id}/answer" not in c.get("/").text


def test_results_rules(login, db):
    s = _open(db)
    hrm = db.scalar(select(User).where(User.username == "hr.manager"))
    head = db.scalar(select(User).where(User.username == "prod.head"))
    r = P.results(db, hrm, s)
    assert r.n >= MIN_GROUP and r.by_q and r.comments
    assert all(x["n"] >= MIN_GROUP for x in r.by_dept.values())
    rh = P.results(db, head, s)
    assert rh.comments == [] and set(rh.by_dept) <= {"PRD"}
    assert login("sup.dry").get(f"/pulse?survey={s.id}").text.count("By question") == 0  # supervisors answer, they do not see results


def test_new_survey_invites_groups(login, db):
    t = date.today()
    login("hr.manager").post("/pulse", {"title": "Test pulse", "opens": str(t), "closes": str(t + timedelta(days=7)), "groups": ["Manager"]})
    s = db.scalar(select(PulseSurvey).where(PulseSurvey.title == "Test pulse"))
    inv = list(db.scalars(select(PulseInvite).where(PulseInvite.survey_id == s.id)))
    assert inv and len(inv) < 15
    assert P.results(db, db.scalar(select(User).where(User.username == "hr.manager")), s).by_q == {}  # nothing shown under 5 answers
