from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Application, ApprovalRequest, Candidate, Employee, Interview, Job, OrgUnit, Position, Requisition
from ..models.core import EMPLOYEE_GROUPS, NATIONALITIES
from ..models.recruit import (
    ACTIVE_STAGES,
    INTERVIEW_KINDS,
    RECOMMEND,
    REJECT_REASONS,
    REQ_REASONS,
    SOURCES,
    STAGES,
    WITHDRAW_REASONS,
)
from ..security import check_csrf, require
from ..services import recruit as R
from ..services.common import user_scope
from ..services.people import PeopleError, parse_date
from .templating import flash, redirect, render

router = APIRouter(prefix="/recruit")
HR = ("hr", "hr_manager")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "supervisor")
CTX = {"stages": STAGES, "sources": SOURCES, "reasons": REQ_REASONS, "reject_reasons": REJECT_REASONS, "withdraw_reasons": WITHDRAW_REASONS,
       "recommend": RECOMMEND, "kinds": INTERVIEW_KINDS, "groups": EMPLOYEE_GROUPS, "nationalities": NATIONALITIES, "active_stages": ACTIVE_STAGES}


def _req(db: Session, rid: int, user) -> Requisition:
    req = db.get(Requisition, rid)
    if req is None or not R.can_see_req(db, user, req):
        raise HTTPException(404)
    return req


def _app(db: Session, aid: int, user) -> Application:
    app = db.get(Application, aid)
    if app is None or not R.can_see_req(db, user, app.requisition):
        raise HTTPException(404)
    return app


def _units(db: Session, user) -> list[OrgUnit]:
    scope = user_scope(db, user)
    q = select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.sort_order, OrgUnit.code)
    return [u for u in db.scalars(q) if scope.all or u.code in scope.org_units]


@router.get("")
def home(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    reqs = R.visible_reqs(db, user)
    st = R.stats(db, reqs)
    today = date.today()
    my_interviews = []
    if user.employee_id:
        my_interviews = list(db.scalars(select(Interview).where(Interview.interviewer_employee_id == user.employee_id, Interview.done_at.is_(None))
                                        .order_by(Interview.when)))
    from ..i18n import gettext
    from .templating import lang_of

    t = lambda x: gettext(lang_of(request), x)
    return render(request, "recruit/home.html", {**CTX, "reqs": reqs, "st": st,
                                                 "funnel_rows": [(t(STAGES[s]), n) for s, n in st.funnel],
                                                 "notsel_rows": [(t(REJECT_REASONS.get(k, k or "-")), n) for k, n in st.not_selected],
                                                 "withdrew_rows": [(t(WITHDRAW_REASONS.get(k, k or "-")), n) for k, n in st.withdrew], "today": today, "is_hr": user.has(*HR), "my_interviews": my_interviews})


@router.get("/new")
def new_form(request: Request, position: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    units = _units(db, user)
    codes = {u.code for u in units}
    positions = [p for p in db.scalars(select(Position).where(Position.active.is_(True)).order_by(Position.org_unit_code, Position.position_id)) if p.org_unit_code in codes]
    pos = db.get(Position, position) if position else None
    free = {p.position_id: R.seats_free(db, p) for p in positions}
    leavers = list(db.scalars(select(Employee).where(Employee.org_unit_code.in_(codes or {"-"}), or_(Employee.status != "Active", Employee.exit_date.is_not(None)))
                              .order_by(Employee.exit_date.desc().nullslast()).limit(40)))
    return render(request, "recruit/new.html", {**CTX, "units": units, "positions": positions, "pos": pos, "free": free, "leavers": leavers,
                                                "jobs": list(db.scalars(select(Job).where(Job.active.is_(True)).order_by(Job.code))), "form": {}})


def _req_data(f) -> dict:
    return {"org_unit_code": f.get("org_unit_code"), "position_id": f.get("position_id") or None, "job_code": f.get("job_code"),
            "employee_group": f.get("employee_group"), "headcount": f.get("headcount") or 1, "reason": f.get("reason"),
            "replaces_employee_id": f.get("replaces_employee_id") or None, "needed_by": parse_date(f.get("needed_by")),
            "requirements": f.get("requirements"), "note": f.get("note")}


@router.post("/check", dependencies=[Depends(check_csrf)], response_class=HTMLResponse)
async def check(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    from html import escape

    from ..i18n import gettext, rule_text
    from .templating import lang_of

    f = await request.form()
    lang = lang_of(request)
    data = _req_data(f)
    if not data["job_code"] or not data["needed_by"]:
        return '<p class="note">…</p>'
    found = R.req_checks(db, data)
    if not found:
        return f'<ul class="find"><li class="ok">{escape(gettext(lang, "Within budget, nothing to check"))}</li></ul>'
    return '<ul class="find">' + "".join(f'<li class="{c["level"]}">{escape(rule_text(lang, c))}</li>' for c in found) + "</ul>"


@router.post("/new", dependencies=[Depends(check_csrf)])
async def new_save(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    f = await request.form()
    try:
        req = R.request_people(db, user, _req_data(f))
        db.commit()
    except (R.RecruitError, ValueError) as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect("/recruit/new" + (f"?position={f.get('position_id')}" if f.get("position_id") else ""))
    flash(request, f"{req.ref} sent for approval")
    return redirect(f"/recruit/req/{req.id}")


@router.get("/req/{rid}")
def req_page(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    req = _req(db, rid, user)
    appr = db.get(ApprovalRequest, req.approval_id) if req.approval_id else None
    board = {s: [a for a in req.applications if a.stage == s] for s in STAGES}
    pool = []
    if user.has(*HR) and req.status == "open":
        on = {a.candidate_id for a in req.applications}
        pool = [c for c in db.scalars(select(Candidate).where(Candidate.erased_at.is_(None)).order_by(Candidate.id.desc()).limit(300)) if c.id not in on]
    from ..i18n import rule_text
    from .templating import lang_of

    checks = [rule_text(lang_of(request), c) for c in (appr.payload or {}).get("checks", [])] if appr else []
    summaries = {a.id: R.interview_summary(a) for a in req.applications}
    return render(request, "recruit/req.html", {**CTX, "req": req, "appr": appr, "board": board, "pool": pool, "is_hr": user.has(*HR),
                                                "checks": checks, "summaries": summaries, "criteria": R.criteria(db, req)})


@router.post("/req/{rid}/ad", dependencies=[Depends(check_csrf)])
async def req_ad(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    req = _req(db, rid, user)
    f = await request.form()
    if f.get("do") == "ai":
        err = R.draft_ad(db, req)
        db.commit()
        flash(request, err or "AI drafted the ad. Check every line before you post it.", "bad" if err else "warn")
    else:
        req.ad_th, req.ad_en = (f.get("ad_th") or "")[:4000] or None, (f.get("ad_en") or "")[:4000] or None
        req.channels = [c for c in f.getlist("channels") if c in SOURCES]
        db.commit()
        flash(request, "Ad saved")
    return redirect(f"/recruit/req/{rid}#ad")


@router.post("/req/{rid}/status", dependencies=[Depends(check_csrf)])
def req_status(request: Request, rid: int, status: str = Form(...), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require(*HR))):
    req = _req(db, rid, user)
    try:
        R.set_status(db, user, req, status, note)
        db.commit()
    except R.RecruitError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/recruit/req/{rid}")


@router.post("/req/{rid}/add", dependencies=[Depends(check_csrf)])
def req_add(request: Request, rid: int, candidate_id: int = Form(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    req = _req(db, rid, user)
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404)
    try:
        app = R.apply(db, user, cand, req)
        db.commit()
    except R.RecruitError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect(f"/recruit/req/{rid}")
    return redirect(f"/recruit/app/{app.id}")


# ---------- candidates ----------

@router.get("/candidates")
def candidates(request: Request, q: str = "", pool: int = 0, db: Session = Depends(get_db), user=Depends(require(*HR))):
    stmt = select(Candidate).order_by(Candidate.id.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Candidate.full_name_en.ilike(like), Candidate.full_name_th.ilike(like), Candidate.phone.ilike(like), Candidate.ref.ilike(like)))
    if pool:
        stmt = stmt.where(Candidate.talent_pool.is_(True), Candidate.erased_at.is_(None))
    rows = list(db.scalars(stmt.limit(300)))
    reqs = [r for r in R.visible_reqs(db, user) if r.status == "open"]
    return render(request, "recruit/candidates.html", {**CTX, "rows": rows, "q": q, "pool": pool, "reqs": reqs, "today": date.today()})


@router.get("/candidates/new")
def cand_new(request: Request, req: int = 0, db: Session = Depends(get_db), user=Depends(require(*HR))):
    return render(request, "recruit/candidate.html", {**CTX, "c": None, "low": set(), "checks": [], "req_id": req,
                                                      "reqs": [r for r in R.visible_reqs(db, user) if r.status == "open"]})


@router.post("/candidates/read", dependencies=[Depends(check_csrf)])
async def cand_read(request: Request, file: UploadFile = File(...), source: str = Form("walk_in"), req: int = Form(0),
                    db: Session = Depends(get_db), user=Depends(require(*HR))):
    data = await file.read()
    try:
        cand = R.read_cv(db, user, data, file.content_type or "", file.filename or "cv", source)
        db.commit()
    except R.RecruitError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect("/recruit/candidates/new" + (f"?req={req}" if req else ""))
    flash(request, "AI read the document. Check the yellow fields against it, record consent, then save.", "warn")
    return redirect(f"/recruit/candidates/{cand.id}" + (f"?req={req}" if req else ""))


def _cand_form(f) -> dict:
    return {k: f.get(k) for k in R.EDITABLE if k in f}


@router.post("/candidates/new", dependencies=[Depends(check_csrf)])
async def cand_create(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    f = await request.form()
    return _save(request, db, user, None, f)


@router.get("/candidates/{cid}")
def cand_page(request: Request, cid: int, req: int = 0, db: Session = Depends(get_db), user=Depends(require(*HR))):
    c = db.get(Candidate, cid)
    if c is None:
        raise HTTPException(404)
    checks = R.candidate_checks(db, {k: getattr(c, k) for k in ("id_card_no", "phone", "nationality", "full_name_en", "birth_date", "previous_employee_id")}, c.id) if not c.erased_at else []
    return render(request, "recruit/candidate.html", {**CTX, "c": c, "low": R.low_confidence(c), "checks": checks, "req_id": req,
                                                      "reqs": [r for r in R.visible_reqs(db, user) if r.status == "open"]})


@router.post("/candidates/{cid}", dependencies=[Depends(check_csrf)])
async def cand_save(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    c = db.get(Candidate, cid)
    if c is None or c.erased_at:
        raise HTTPException(404)
    f = await request.form()
    if f.get("do") == "erase":
        try:
            R.erase(db, user, c)
            db.commit()
            flash(request, "Personal data erased")
        except R.RecruitError as e:
            db.rollback()
            flash(request, str(e), "bad")
        return redirect(f"/recruit/candidates/{cid}")
    return _save(request, db, user, c, f)


def _save(request: Request, db: Session, user, c, f):
    try:
        c = R.save_candidate(db, user, _cand_form(f), c, consent=f.get("consent") or None, talent_pool=f.get("talent_pool") == "on")
        if c.ai_used and c.ai_confidence:
            c.ai_confidence = {k: 1.0 for k in c.ai_confidence}  # HR has checked every field against the document
        rid = int(f.get("req_id") or 0)
        app = None
        if rid:
            req = db.get(Requisition, rid)
            if req is not None and not db.scalar(select(Application).where(Application.requisition_id == rid, Application.candidate_id == c.id)):
                app = R.apply(db, user, c, req)
        db.commit()
    except (R.RecruitError, ValueError) as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect(f"/recruit/candidates/{c.id}" if c is not None and c.id else "/recruit/candidates/new")
    flash(request, "Candidate saved")
    return redirect(f"/recruit/app/{app.id}" if app else f"/recruit/candidates/{c.id}")


@router.get("/candidates/{cid}/file")
def cand_file(cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    c = db.get(Candidate, cid)
    if c is None or not c.file_path:
        raise HTTPException(404)
    p = (settings.storage_dir / c.file_path).resolve()
    if settings.storage_dir.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=c.file_mime, headers={"Cache-Control": "private, no-store"})


# ---------- applications ----------

@router.get("/app/{aid}")
def app_page(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    app = _app(db, aid, user)
    is_hr = user.has(*HR)
    hire = R.hire_data(db, app) if is_hr and app.stage == "offer" and app.offer_status == "accepted" else None
    hire_appr = db.get(ApprovalRequest, app.hire_approval_id) if app.hire_approval_id else None
    interviewers = list(db.scalars(select(Employee).where(Employee.status == "Active", Employee.employee_group != "Operator").order_by(Employee.employee_id)))
    return render(request, "recruit/app.html", {**CTX, "a": app, "c": app.candidate, "req": app.requisition, "is_hr": is_hr, "hire": hire,
                                                "hire_appr": hire_appr, "summary": R.interview_summary(app), "interviewers": interviewers,
                                                "units": {u.code: u for u in db.scalars(select(OrgUnit))}})


@router.post("/app/{aid}", dependencies=[Depends(check_csrf)])
async def app_action(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    app = _app(db, aid, user)
    f = await request.form()
    do = f.get("do")
    try:
        if do == "interview":
            when = datetime.fromisoformat(f.get("when") or "")
            if not (user.has(*HR) or R.can_see_req(db, user, app.requisition)):
                raise HTTPException(403)
            iv = R.schedule(db, user, app, when, f.get("kind") if f.get("kind") in INTERVIEW_KINDS else "onsite", f.get("interviewer") or user.employee_id)
            db.commit()
            return redirect(f"/recruit/interview/{iv.id}")
        if not user.has(*HR):
            raise HTTPException(403)
        if do == "move":
            R.move(db, user, app, f.get("stage") or "", f.get("note") or "", f.get("reason") or "")
        elif do == "recheck":
            R.recheck(db, app)
        elif do == "offer":
            R.offer(db, user, app, date.fromisoformat(f.get("start_date") or ""), f.get("note") or "")
        elif do == "accept":
            R.offer_answer(db, user, app, True)
        elif do == "decline":
            R.offer_answer(db, user, app, False, f.get("reason") or "other", f.get("note") or "")
        elif do == "hire":
            data = R.hire_data(db, app)
            for k in ("employee_id", "title", "gender", "full_name_en", "full_name_th", "nickname", "join_date", "probation_end_date", "pay_type"):
                if f.get(k) is not None:
                    data[k] = f.get(k) or None
            for k in ("join_date", "probation_end_date"):
                data[k] = parse_date(data.get(k))
            R.hire(db, user, app, data)
            db.commit()
            flash(request, "New-employee request sent. A second HR person approves it, then onboarding opens.")
            return redirect(f"/recruit/app/{aid}")
        else:
            raise HTTPException(400)
        db.commit()
    except (R.RecruitError, PeopleError, ValueError) as e:
        db.rollback()
        flash(request, str(e) or "Check the date", "bad")
    return redirect(f"/recruit/app/{aid}")


# ---------- interviews ----------

def _iv(db: Session, iid: int, user) -> Interview:
    iv = db.get(Interview, iid)
    if iv is None or not (R.can_see_req(db, user, iv.application.requisition) or iv.interviewer_employee_id == user.employee_id):
        raise HTTPException(404)
    return iv


@router.get("/interview/{iid}")
def interview_page(request: Request, iid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    iv = _iv(db, iid, user)
    crit = {c["code"]: c for c in R.criteria(db, iv.application.requisition)}
    return render(request, "recruit/interview.html", {**CTX, "iv": iv, "a": iv.application, "c": iv.application.candidate, "crit": crit,
                                                      "can_score": R.can_score(db, user, iv), "is_hr": user.has(*HR)})


@router.post("/interview/{iid}", dependencies=[Depends(check_csrf)])
async def interview_save(request: Request, iid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    iv = _iv(db, iid, user)
    if not R.can_score(db, user, iv):
        raise HTTPException(403)
    f = await request.form()
    if f.get("do") == "suggest":
        err = R.suggest_questions(db, iv)
        db.commit()
        flash(request, err or "AI suggested questions for this candidate. Use the ones that fit.", "bad" if err else "warn")
        return redirect(f"/recruit/interview/{iid}")
    try:
        R.score(db, user, iv, {k[2:]: v for k, v in f.items() if k.startswith("s_")}, {k[2:]: v for k, v in f.items() if k.startswith("e_")},
                f.get("recommendation") or "", f.get("note") or "")
        db.commit()
        flash(request, "Scorecard saved")
        return redirect(f"/recruit/app/{iv.application_id}")
    except R.RecruitError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect(f"/recruit/interview/{iid}")
