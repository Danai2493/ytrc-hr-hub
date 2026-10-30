from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApprovalRequest, OnboardingCase, ProbationReview
from ..models.develop import PROBATION_CRITERIA
from ..security import check_csrf, require
from ..services import approvals
from ..services import newstaff as N
from ..services.common import user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/newstaff")
WHO = ("hr", "hr_manager", "dept_head", "supervisor", "admin", "gm")
HR = ("hr", "hr_manager")


def _sees(db: Session, user, emp) -> bool:
    scope = user_scope(db, user)
    return scope.all or user.has("admin") or emp.org_unit_code in scope.org_units


@router.get("")
def home(request: Request, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    if N.sync_reviews(db):
        db.commit()
    onb = [c for c in db.scalars(select(OnboardingCase).where(OnboardingCase.status == "open").order_by(OnboardingCase.start_date.desc())) if _sees(db, user, c.employee)]
    rvs = [r for r in db.scalars(select(ProbationReview).where(ProbationReview.status.in_(("open", "submitted"))).order_by(ProbationReview.probation_end))
           if _sees(db, user, r.employee)]
    done = [r for r in db.scalars(select(ProbationReview).where(ProbationReview.status.in_(("passed", "failed"))).order_by(ProbationReview.probation_end.desc()).limit(30))
            if _sees(db, user, r.employee)]
    return render(request, "newstaff/home.html", {"onb": onb, "rvs": rvs, "done": done, "today": date.today()})


@router.get("/onboarding/{cid}")
def onboarding(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    c = db.get(OnboardingCase, cid)
    if c is None or not _sees(db, user, c.employee):
        raise HTTPException(404)
    return render(request, "newstaff/onboarding.html", {"c": c, "can_tick": {i["code"]: N.can_tick(user, i) for i in c.items}, "today": date.today()})


@router.post("/onboarding/{cid}/tick", dependencies=[Depends(check_csrf)])
def onboarding_tick(request: Request, cid: int, code: str = Form(...), done: str = Form(""), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require(*WHO))):
    c = db.get(OnboardingCase, cid)
    if c is None or not _sees(db, user, c.employee):
        raise HTTPException(404)
    try:
        N.tick(db, c, user, code, done == "1", note)
        db.commit()
    except PermissionError:
        db.rollback()
        flash(request, "This item is ticked by someone else", "bad")
    return redirect(f"/newstaff/onboarding/{cid}")


@router.get("/probation/{rid}")
def probation(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    rv = db.get(ProbationReview, rid)
    if rv is None or not _sees(db, user, rv.employee):
        raise HTTPException(404)
    facts = rv.facts or N.facts(db, rv.employee, date.today())
    appr = db.get(ApprovalRequest, rv.approval_id) if rv.approval_id else None
    can_fill = rv.status == "open" and user.has("dept_head", "supervisor", "hr", "hr_manager")
    return render(request, "newstaff/probation.html", {"rv": rv, "facts": facts, "criteria": PROBATION_CRITERIA, "appr": appr, "can_fill": can_fill,
                                                       "can": appr and approvals.can_decide(user, appr), "decide_by": N.decide_by(rv.employee),
                                                       "today": date.today()})


@router.post("/probation/{rid}", dependencies=[Depends(check_csrf)])
async def probation_submit(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require("dept_head", "supervisor", "hr", "hr_manager"))):
    rv = db.get(ProbationReview, rid)
    if rv is None or not _sees(db, user, rv.employee):
        raise HTTPException(404)
    f = await request.form()
    try:
        N.submit(db, rv, user, {k: f.get(f"s_{k}") for k, _ in PROBATION_CRITERIA}, f.get("recommendation") or "", f.get("comment") or "")
        db.commit()
        flash(request, "Probation review sent for approval")
    except (N.ProbationError, approvals.ApprovalError) as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/newstaff/probation/{rid}")
