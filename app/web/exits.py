from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ExitCase
from ..models.core import EXIT_REASONS
from ..models.hrdev import EXIT_INTERVIEW_QUESTIONS
from ..security import check_csrf, require
from ..services import exits as X
from ..services.common import user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/exit")
WHO = ("hr", "hr_manager", "dept_head", "supervisor", "admin")
HR = ("hr", "hr_manager")


def _get(db: Session, cid: int, user) -> ExitCase:
    case = db.get(ExitCase, cid)
    if case is None:
        raise HTTPException(404)
    scope = user_scope(db, user)
    if not (scope.all or user.has("admin") or case.employee.org_unit_code in scope.org_units):
        raise HTTPException(404)
    return case


@router.get("")
def exit_list(request: Request, status: str = "open", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    q = select(ExitCase).order_by(ExitCase.last_day.desc()).limit(300)
    if status:
        q = q.where(ExitCase.status == status)
    scope = user_scope(db, user)
    rows = [c for c in db.scalars(q) if scope.all or user.has("admin") or c.employee.org_unit_code in scope.org_units]
    return render(request, "exit/list.html", {"rows": rows, "status": status, "today": date.today(), "reasons": dict(EXIT_REASONS)})


@router.get("/{cid}")
def exit_detail(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    case = _get(db, cid, user)
    return render(request, "exit/detail.html", {
        "c": case, "can_tick": {i["code"]: X.can_tick(user, i) for i in case.items}, "is_hr": user.has(*HR),
        "questions": EXIT_INTERVIEW_QUESTIONS, "reasons": EXIT_REASONS, "reason_names": dict(EXIT_REASONS),
        "sso_due": X.sso_due(db, case.last_day), "today": date.today(),
    })


@router.post("/{cid}/tick", dependencies=[Depends(check_csrf)])
def exit_tick(request: Request, cid: int, code: str = Form(...), done: str = Form(""), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require(*WHO))):
    case = _get(db, cid, user)
    if case.status != "open":
        raise HTTPException(400)
    try:
        X.tick(db, case, user, code, done == "1", note)
        db.commit()
    except PermissionError:
        db.rollback()
        flash(request, "This item is ticked by someone else", "bad")
    return redirect(f"/exit/{cid}")


@router.post("/{cid}/close", dependencies=[Depends(check_csrf)])
def exit_close(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    case = _get(db, cid, user)
    try:
        X.close(db, case, user)
        db.commit()
        flash(request, f"{case.ref} closed")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/exit/{cid}")


@router.post("/{cid}/interview", dependencies=[Depends(check_csrf)])
async def exit_interview(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    case = _get(db, cid, user)
    f = await request.form()
    X.save_interview(db, case, user, {q: f.get(f"s_{q}") for q, _ in EXIT_INTERVIEW_QUESTIONS},
                     f.get("would_return") or "", f.get("main_reason") or "", f.get("comments") or "")
    err = X.suggest_reason(db, case) if f.get("ask_ai") else None
    db.commit()
    flash(request, err or "Interview saved", "bad" if err else "ok")
    return redirect(f"/exit/{cid}#interview")


@router.post("/{cid}/suggest", dependencies=[Depends(check_csrf)])
def exit_suggest(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    case = _get(db, cid, user)
    err = X.suggest_reason(db, case)
    db.commit()
    if err:
        flash(request, err, "bad")
    return redirect(f"/exit/{cid}#interview")


@router.post("/{cid}/reason", dependencies=[Depends(check_csrf)])
def exit_reason(request: Request, cid: int, code: str = Form(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    case = _get(db, cid, user)
    try:
        X.confirm_reason(db, case, user, code)
        db.commit()
        flash(request, "Exit reason confirmed")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/exit/{cid}#interview")
