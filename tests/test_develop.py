"""Phase 2, part 2: skills matrix, training, job descriptions, onboarding, probation."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    ApprovalRequest,
    Employee,
    EmployeeSkill,
    EmploymentEvent,
    ExitCase,
    OnboardingCase,
    ProbationReview,
    TrainingNeed,
    TrainingRecord,
)


def test_matrix_page_and_set_level(login, db):
    sup = login("sup.dry")
    r = sup.get("/skills?unit=PRD-DRY")
    assert r.status_code == 200 and "Dryer operation" in r.text
    assert sup.get("/skills?unit=PRD-PCL").text.count("YTOP9") == 0  # other unit is out of scope
    r = sup.post("/skills/set", {"employee_id": "YTOP951", "skill_code": "SK-DRY-01", "level": "4", "note": "Watched one night shift"})
    assert r.status_code == 303
    db.expire_all()
    es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == "YTOP951", EmployeeSkill.skill_code == "SK-DRY-01"))
    assert es.level == 4 and es.history[-1]["note"] == "Watched one night shift"
    # a supervisor cannot set levels outside the team
    assert sup.post("/skills/set", {"employee_id": "YTR813", "skill_code": "SK-QA-01", "level": "3"}).status_code == 404


def test_gap_becomes_need_and_class_closes_it(login, db):
    from app.services import skills

    db.expire_all()
    emp = db.get(Employee, "YTOP952")
    es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == "YTOP952", EmployeeSkill.skill_code == "SK-SAF-02"))
    if es:
        db.delete(es)
        db.commit()
    skills.generate_needs(db, emp_ids={"YTOP952"})
    db.commit()
    need = db.scalar(select(TrainingNeed).where(TrainingNeed.employee_id == "YTOP952", TrainingNeed.skill_code == "SK-SAF-02", TrainingNeed.status == "open"))
    assert need is not None and need.reason in ("legal", "expiry") and need.course_code == "CR-SAF-02"
    hr = login("hr.officer")
    page = hr.get("/skills/training/new?course=CR-SAF-02")
    assert 'value="YTOP952" checked' in page.text  # people who need the course are ticked
    r = hr.post("/skills/training/new", {"course": "CR-SAF-02", "on_date": date.today().isoformat(), "emp": ["YTOP952"], "trainer": "Safety officer"})
    assert r.status_code == 303
    db.expire_all()
    assert db.get(TrainingNeed, need.id).status == "done"
    es = db.scalar(select(EmployeeSkill).where(EmployeeSkill.employee_id == "YTOP952", EmployeeSkill.skill_code == "SK-SAF-02"))
    assert es.level == 3 and es.valid_until > date.today() + timedelta(days=300)  # LOTO refresher every 12 months
    assert db.scalar(select(TrainingRecord).where(TrainingRecord.employee_id == "YTOP952", TrainingRecord.course_code == "CR-SAF-02", TrainingRecord.result == "passed"))


def test_lapsed_licence_counts_as_L(db):
    from app.services import skills

    es = EmployeeSkill(employee_id="YTOP951", skill_code="SK-FLT-01", level=3, valid_until=date.today() - timedelta(days=1))
    assert skills.effective_level(es, date.today()) == 2


def test_job_description_ai_draft_and_save(login, db):
    hr = login("hr.manager")
    r = hr.post("/skills/jobs/OPR-DRY/draft")
    assert r.status_code == 200 and "AI draft below" in r.text and "Prepare and run" in r.text
    hr.post("/skills/jobs/OPR-DRY", {"purpose": "Runs the dryer line safely (SAMPLE)", "responsibilities": "Check temperature\nPress bales",
                                     "education": "Grade 6", "experience": "None", "lvl_SK-DRY-01": "3", "lvl_SK-MIX-01": "2"})
    db.expire_all()
    from app.models import Job, JobSkill

    assert db.get(Job, "OPR-DRY").purpose.startswith("Runs the dryer")
    assert db.get(JobSkill, ("OPR-DRY", "SK-MIX-01")).level == 2
    assert login("sup.dry").post("/skills/jobs/OPR-DRY/draft").status_code == 403


def test_hire_opens_onboarding(login, db):
    hr, hr2 = login("hr.officer"), login("hr.officer2")
    r = hr.post("/people/new", {"employee_id": "YTOP995", "employee_group": "Operator", "title": "Ms.", "full_name_en": "New Starter (SAMPLE)",
                                "nationality": "Myanmar", "birth_date": "2000-01-01", "join_date": date.today().isoformat(), "org_unit_code": "PRD-DRY",
                                "job_code": "OPR-DRY", "grade_code": "Operator", "pay_type": "Daily"})
    assert r.status_code == 303, r.text[:500]
    appr = db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.id.desc())).first()
    hr2.post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    case = db.scalar(select(OnboardingCase).where(OnboardingCase.employee_id == "YTOP995"))
    assert case is not None
    codes = [i["code"] for i in case.items]
    assert "SSO" in codes and "PERMIT" in codes and "IT" not in codes
    assert db.scalar(select(TrainingNeed).where(TrainingNeed.employee_id == "YTOP995", TrainingNeed.reason == "new_staff"))
    hr.post(f"/newstaff/onboarding/{case.id}/tick", {"code": "SSO", "done": "1"})
    db.expire_all()
    assert next(i for i in db.get(OnboardingCase, case.id).items if i["code"] == "SSO")["done"]


def test_probation_pass_and_fail(login, db):
    from app.services import newstaff

    db.expire_all()
    for eid, days in (("YTOP960", 100), ("YTOP961", 105)):
        e = db.get(Employee, eid)
        e.join_date = date.today() - timedelta(days=days)
        e.probation_end_date = e.join_date + timedelta(days=118)
    db.commit()
    newstaff.sync_reviews(db)
    db.commit()
    sup, head, hr, mgr = login("sup.dry"), login("prod.head"), login("hr.officer"), login("hr.manager")
    scores = {f"s_{k}": "4" for k in ("quality", "speed", "safety", "discipline", "teamwork", "learning")}
    rv = db.scalar(select(ProbationReview).where(ProbationReview.employee_id == "YTOP960"))
    if db.get(Employee, "YTOP960").org_unit_code != "PRD-DRY":
        sup = head
    r = sup.post(f"/newstaff/probation/{rv.id}", {**scores, "recommendation": "pass", "comment": "Learns fast"})
    assert r.status_code == 303
    db.expire_all()
    rv = db.get(ProbationReview, rv.id)
    assert rv.status == "submitted" and rv.facts["workdays"] >= 0
    for c in (head, hr):
        c.post(f"/approvals/{rv.approval_id}/decide", {"decision": "approved"})
    db.expire_all()
    assert db.get(ProbationReview, rv.id).status == "passed"
    assert db.scalar(select(EmploymentEvent).where(EmploymentEvent.employee_id == "YTOP960", EmploymentEvent.event_type == "probation_pass"))
    # a fail needs reasons, then the HR Manager, and opens the exit case
    rv2 = db.scalar(select(ProbationReview).where(ProbationReview.employee_id == "YTOP961"))
    r = head.post(f"/newstaff/probation/{rv2.id}", {**scores, "recommendation": "fail", "comment": ""})
    db.expire_all()
    assert db.get(ProbationReview, rv2.id).status == "open"
    head.post(f"/newstaff/probation/{rv2.id}", {**scores, "s_safety": "1", "recommendation": "fail", "comment": "Ignored lock-out twice (SAMPLE)"})
    db.expire_all()
    rv2 = db.get(ProbationReview, rv2.id)
    appr = db.get(ApprovalRequest, rv2.approval_id)
    assert [s.name for s in appr.steps][-1] == "hr_manager"
    for c in (hr, mgr):
        c.post(f"/approvals/{appr.id}/decide", {"decision": "approved"})
    db.expire_all()
    assert db.get(ProbationReview, rv2.id).status == "failed"
    case = db.scalar(select(ExitCase).where(ExitCase.employee_id == "YTOP961"))
    assert case is not None and case.reason_code == "PROBATION"


def test_probation_fail_too_late_is_blocked(db):
    from app.models import User
    from app.services import newstaff

    e = db.get(Employee, "YTOP962")
    e.join_date = date.today() - timedelta(days=200)
    e.probation_end_date = None
    rv = ProbationReview(ref="PR-TEST-1", employee_id="YTOP962", probation_end=e.join_date + timedelta(days=118))
    rv.employee = e
    db.add(rv)
    db.flush()
    u = db.scalar(select(User).where(User.username == "prod.head"))
    import pytest

    with pytest.raises(newstaff.ProbationError, match="severance"):
        newstaff.submit(db, rv, u, {k: 3 for k in ("quality", "speed", "safety", "discipline", "teamwork", "learning")}, "fail", "late")
    db.rollback()


def test_phase2b_pages_render(login):
    hr = login("hr.manager")
    for url in ("/skills", "/skills/needs", "/skills/training", "/skills/training/new", "/skills/jobs", "/skills/jobs/OPR-WH",
                "/skills/catalog", "/skills/person/YTOP951", "/newstaff"):
        assert hr.get(url).status_code == 200, url
    assert login("sup.pcl").get("/newstaff").status_code == 200
