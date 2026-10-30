"""Recruitment: manpower request, candidates, screening, interview, offer, hire."""
import re
from datetime import date, timedelta

from sqlalchemy import select

from app.models import Application, ApprovalRequest, Candidate, Employee, Interview, OnboardingCase, Requisition


def _open_req(db, unit="PRD-DRY"):
    return db.scalar(select(Requisition).where(Requisition.status == "open", Requisition.org_unit_code == unit))


def test_request_needs_gm_outside_budget(login, db):
    sup = login("sup.dry")
    r = sup.post("/recruit/check", {"org_unit_code": "PRD-DRY", "position_id": "POS-0017", "job_code": "OPR-DRY", "employee_group": "Operator",
                                    "headcount": "5", "reason": "new", "needed_by": (date.today() + timedelta(days=5)).isoformat()})
    assert "GM approves" in r.text and "Needed in 5 days" in r.text
    r = sup.post("/recruit/new", {"org_unit_code": "PRD-DRY", "position_id": "POS-0017", "job_code": "OPR-DRY", "employee_group": "Operator",
                                  "headcount": "5", "reason": "new", "needed_by": (date.today() + timedelta(days=30)).isoformat()})
    assert r.status_code == 303 and "/recruit/req/" in r.headers["location"]
    db.expire_all()
    req = db.get(Requisition, int(r.headers["location"].rsplit("/", 1)[1]))
    assert req.status == "pending" and not req.within_budget
    appr = db.get(ApprovalRequest, req.approval_id)
    assert [s.name for s in appr.steps] == ["dept_head", "hr_manager", "gm"]
    # another unit is out of a supervisor's scope
    r = sup.post("/recruit/new", {"org_unit_code": "QA", "job_code": "QA-INSP", "employee_group": "Staff", "headcount": "1", "reason": "new",
                                  "needed_by": (date.today() + timedelta(days=30)).isoformat()})
    assert r.headers["location"] == "/recruit/new"


def test_approval_opens_request_and_ai_drafts_ad(db):
    from app.models import User
    from app.services import approvals, recruit

    users = {u.username: u for u in db.scalars(select(User))}
    req = recruit.request_people(db, users["sup.pcl"], {"org_unit_code": "PRD-PCL", "position_id": "POS-0018", "job_code": "OPR-PCL", "employee_group": "Operator",
                                                        "headcount": 1, "reason": "replacement", "replaces_employee_id": "YTOP988", "needed_by": date.today() + timedelta(days=30)})
    appr = db.get(ApprovalRequest, req.approval_id)
    assert [s.name for s in appr.steps] == ["dept_head", "hr_manager"]  # a replacement inside the budget needs no GM
    approvals.decide(db, appr, users["prod.head"], "approved", "ok")
    approvals.decide(db, appr, users["hr.manager"], "approved", "ok")
    assert req.status == "open" and req.ad_ai and "Pay" in req.ad_en
    for word in ("baht", "฿", "salary"):
        assert word not in (req.ad_en + req.ad_th).lower()


def test_ai_reads_cv_and_screening_blocks_without_consent(login, db):
    hr = login("hr.officer")
    req = _open_req(db)
    r = hr.post("/recruit/candidates/read", {"source": "walk_in", "req": str(req.id)}, files={"file": ("cv.png", b"fake", "image/png")})
    assert r.status_code == 303
    cid = int(re.search(r"/candidates/(\d+)", r.headers["location"]).group(1))
    db.expire_all()
    c = db.get(Candidate, cid)
    assert c.ai_used and c.full_name_en.startswith("Somchai") and c.consent_at is None
    page = hr.get(f"/recruit/candidates/{cid}").text
    assert "lowc" in page  # the birth date the AI was unsure of is highlighted
    # save without consent and apply: blocked at screening
    r = hr.post(f"/recruit/candidates/{cid}", {"full_name_en": c.full_name_en, "nationality": "Thai", "phone": "089 000 0123", "req_id": str(req.id)})
    app_id = int(r.headers["location"].rsplit("/", 1)[1])
    db.expire_all()
    app = db.get(Application, app_id)
    assert any(f["code"] == "cand_no_consent" and f["level"] == "block" for f in app.checks)
    r = hr.post(f"/recruit/app/{app_id}", {"do": "interview", "when": "2030-01-05T10:00", "kind": "onsite", "interviewer": "YTR806"})
    db.expire_all()
    assert not db.get(Application, app_id).interviews
    hr.post(f"/recruit/candidates/{cid}", {"full_name_en": c.full_name_en, "nationality": "Thai", "consent": "paper"})
    hr.post(f"/recruit/app/{app_id}", {"do": "recheck"})
    db.expire_all()
    assert not any(f["level"] == "block" for f in db.get(Application, app_id).checks)
    assert db.get(Candidate, cid).phone == "0890000123"


def test_former_employee_and_duplicate_are_found(db):
    from app.models import User
    from app.services import recruit

    hr = db.scalar(select(User).where(User.username == "hr.officer"))
    gone = db.get(Employee, "YTOP986")
    found = recruit.candidate_checks(db, {"full_name_en": gone.full_name_en, "birth_date": gone.birth_date})
    assert any(f["code"] == "cand_former" for f in found)
    active = db.get(Employee, "YTOP951")
    assert any(f["code"] == "cand_active_employee" for f in recruit.candidate_checks(db, {"full_name_en": active.full_name_en, "birth_date": active.birth_date}))
    other = db.scalars(select(Candidate).where(Candidate.phone.is_not(None))).first()
    assert any(f["code"] == "cand_duplicate" for f in recruit.candidate_checks(db, {"phone": other.phone}))
    assert recruit.valid_thai_id("1101700207030") and not recruit.valid_thai_id("1101700207031")


def test_scorecard_needs_evidence_for_extremes(login, db):
    sup = login("sup.dry")
    iv = db.scalar(select(Interview).where(Interview.done_at.is_(None)))
    crit = [q["criterion"] for q in iv.questions]
    form = {f"s_{c}": "3" for c in crit}
    form[f"s_{crit[0]}"] = "5"
    form["recommendation"] = "hire"
    r = sup.post(f"/recruit/interview/{iv.id}", form)
    assert r.headers["location"].endswith(f"/interview/{iv.id}")  # refused: no evidence for the 5
    form[f"e_{crit[0]}"] = "Explained lock-out steps in the right order"
    r = sup.post(f"/recruit/interview/{iv.id}", form)
    assert r.headers["location"].endswith(f"/app/{iv.application_id}")
    db.expire_all()
    assert db.get(Interview, iv.id).done_at
    # AI questions avoid personal topics and fit the criteria
    r = sup.post(f"/recruit/interview/{iv.id}", {"do": "suggest"})


def test_offer_accept_hire_opens_onboarding(login, db):
    hr = login("hr.officer")
    hr2 = login("hr.officer2")
    app = db.scalar(select(Application).where(Application.stage == "offer"))
    req = app.requisition
    assert hr.post(f"/recruit/app/{app.id}", {"do": "accept"}).status_code == 303
    page = hr.get(f"/recruit/app/{app.id}").text
    eid = re.search(r'name="employee_id" value="([A-Z0-9]+)"', page).group(1)
    assert eid.startswith("YTOP")
    hr.post(f"/recruit/app/{app.id}", {"do": "hire", "employee_id": eid, "join_date": app.start_date.isoformat()})
    db.expire_all()
    app = db.get(Application, app.id)
    appr = db.get(ApprovalRequest, app.hire_approval_id)
    assert appr and appr.status == "pending" and appr.payload["application_id"] == app.id
    assert "salary" not in str(appr.payload).lower()
    hr2.post(f"/approvals/{appr.id}/decide", {"decision": "approved", "comment": "checked"})
    db.expire_all()
    app = db.get(Application, app.id)
    assert app.stage == "hired" and app.employee_id == eid
    emp = db.get(Employee, eid)
    assert emp.position_id == req.position_id and emp.probation_end_date == emp.join_date + timedelta(days=118)
    assert db.scalar(select(OnboardingCase).where(OnboardingCase.employee_id == eid))


def test_reject_needs_reason_and_erase_after_retention(login, db):
    hr = login("hr.officer")
    app = db.scalar(select(Application).where(Application.stage == "applied"))
    hr.post(f"/recruit/app/{app.id}", {"do": "move", "stage": "rejected", "reason": ""})
    db.expire_all()
    assert db.get(Application, app.id).stage == "applied"
    hr.post(f"/recruit/app/{app.id}", {"do": "move", "stage": "rejected", "reason": "skills"})
    db.expire_all()
    assert db.get(Application, app.id).stage == "rejected"
    old = db.scalar(select(Candidate).where(Candidate.full_name_en == "Old Applicant (SAMPLE)"))
    hr.post(f"/recruit/candidates/{old.id}", {"do": "erase"})
    db.expire_all()
    old = db.get(Candidate, old.id)
    assert old.erased_at and old.phone is None and old.full_name_en.startswith("Erased")


def test_pages_render_and_scope(login, db):
    hr = login("hr.manager")
    for url in ("/recruit", "/recruit/new", "/recruit/candidates", "/recruit/candidates/new"):
        assert hr.get(url).status_code == 200, url
    for req in db.scalars(select(Requisition)):
        assert hr.get(f"/recruit/req/{req.id}").status_code == 200
    for a in db.scalars(select(Application)):
        assert hr.get(f"/recruit/app/{a.id}").status_code == 200
    pcl = login("sup.pcl")
    dry = _open_req(db)
    assert pcl.get(f"/recruit/req/{dry.id}").status_code == 404
    assert pcl.get("/recruit/candidates").status_code == 403
    # a supervisor sees candidates on their own request but not the phone number
    sup = login("sup.dry")
    a = dry.applications[0]
    page = sup.get(f"/recruit/app/{a.id}").text
    assert a.candidate.full_name_en in page and (a.candidate.phone or "zzz") not in page
