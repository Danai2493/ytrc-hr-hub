from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PulseSurvey
from ..models.pulse import COMMENT, MIN_GROUP, SCALE
from ..security import check_csrf, current_user, require
from ..services import pulse as P
from .templating import flash, lang_of, redirect, render

router = APIRouter(prefix="/pulse")
GROUPS = ["Operator", "Staff", "Manager", "Japanese Management"]


def _survey(db: Session, sid: int) -> PulseSurvey:
    s = db.get(PulseSurvey, sid)
    if s is None:
        raise HTTPException(404)
    return s


@router.get("")
def home(request: Request, survey: int = 0, db: Session = Depends(get_db), user=Depends(current_user)):
    surveys = list(db.scalars(select(PulseSurvey).order_by(PulseSurvey.opens_on.desc())))
    see = P.can_see_results(db, user)
    s = (db.get(PulseSurvey, survey) if survey else (surveys[0] if surveys else None)) if see else None
    t = date.today()
    return render(request, "pulse/home.html", {"surveys": surveys, "s": s, "r": P.results(db, user, s) if s else None, "mine": P.my_open(db, user),
                                               "see": see, "min_group": MIN_GROUP, "names": P.dept_names(db), "lang": lang_of(request), "scale": SCALE,
                                               "comment_q": COMMENT, "groups": GROUPS,
                                               "defaults": {"title": f"Pulse {t.strftime('%b %Y')}", "opens": t, "closes": t + timedelta(days=14)},
                                               "depts": sorted(_dept_codes(db))})


def _dept_codes(db: Session) -> set[str]:
    from ..models import Employee
    return {P.dept_code(e) for e in db.scalars(select(Employee).where(Employee.status == "Active"))} - {None}


@router.post("", dependencies=[Depends(check_csrf)])
async def create(request: Request, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    f = await request.form()
    try:
        s = P.open_survey(db, user, f.get("title") or "", date.fromisoformat(f.get("opens")), date.fromisoformat(f.get("closes")), f.getlist("groups"))
        db.commit()
        flash(request, "Survey opened. Invited people see it on their home page.")
        return redirect(f"/pulse?survey={s.id}")
    except (P.PulseError, ValueError, TypeError) as e:
        db.rollback()
        flash(request, str(e) or "Check the dates", "bad")
        return redirect("/pulse")


@router.get("/{sid}/answer")
def answer_form(request: Request, sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    s = _survey(db, sid)
    if s not in P.my_open(db, user):
        flash(request, "This survey is not open for you", "bad")
        return redirect("/pulse")
    return render(request, "pulse/answer.html", {"s": s, "lang": lang_of(request), "scale": SCALE, "comment_q": COMMENT, "paper": False})


@router.post("/{sid}/answer", dependencies=[Depends(check_csrf)])
async def answer(request: Request, sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    s = _survey(db, sid)
    f = await request.form()
    try:
        P.answer(db, user, s, f)
        db.commit()
        flash(request, "Thank you. Your answers are anonymous.")
        return redirect("/")
    except P.PulseError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect(f"/pulse/{sid}/answer")


@router.get("/{sid}/paper")
def paper_form(request: Request, sid: int, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    s = _survey(db, sid)
    return render(request, "pulse/answer.html", {"s": s, "lang": lang_of(request), "scale": SCALE, "comment_q": COMMENT, "paper": True,
                                                 "names": P.dept_names(db), "depts": sorted(_dept_codes(db)), "groups": GROUPS})


@router.post("/{sid}/paper", dependencies=[Depends(check_csrf)])
async def paper(request: Request, sid: int, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    s = _survey(db, sid)
    f = await request.form()
    try:
        P.paper(db, user, s, f)
        db.commit()
        flash(request, "Paper answer saved. Next sheet.")
    except P.PulseError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/pulse/{sid}/paper")


@router.post("/{sid}/close", dependencies=[Depends(check_csrf)])
def close(request: Request, sid: int, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    P.close(db, user, _survey(db, sid))
    db.commit()
    flash(request, "Survey closed.")
    return redirect(f"/pulse?survey={sid}")
