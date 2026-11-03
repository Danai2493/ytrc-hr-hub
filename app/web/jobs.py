"""Internal job board: open jobs shown to employees first, hands raised, moves started."""
from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, Requisition
from ..models.recruit import INTEREST_STATUSES, InternalInterest
from ..security import check_csrf, current_user, require
from ..services import mobility as J
from ..services.common import audit
from ..services.people import parse_date
from .templating import flash, redirect, render

router = APIRouter()
HR = ("hr", "hr_manager")


@router.get("/jobs")
def board(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    emp = db.get(Employee, user.employee_id) if user.employee_id else None
    posts = J.postings(db)
    mine = J.my_interests(db, emp.employee_id) if emp else {}
    why = {r.id: J.can_raise(db, emp, r) for r in posts}
    history = [ii for ii in mine.values() if ii.requisition not in posts]
    return render(request, "jobs/board.html", {"posts": posts, "mine": mine, "why": why, "history": history, "emp": emp,
                                               "statuses": INTEREST_STATUSES, "is_hr": user.has(*HR)})


@router.post("/jobs/{rid}", dependencies=[Depends(check_csrf)])
def raise_hand(request: Request, rid: int, note: str = Form(""), db: Session = Depends(get_db), user=Depends(current_user)):
    req = db.get(Requisition, rid) or _404()
    try:
        J.raise_hand(db, user, req, note)
        db.commit()
        flash(request, "Thank you. HR will talk with you first, before your supervisor is told.")
    except J.JobsError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect("/jobs")


@router.post("/jobs/interest/{iid}/withdraw", dependencies=[Depends(check_csrf)])
def withdraw(request: Request, iid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    ii = db.get(InternalInterest, iid) or _404()
    try:
        J.withdraw(db, user, ii)
        db.commit()
        flash(request, "You withdrew. Nothing is kept against you.")
    except J.JobsError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect("/jobs")


@router.post("/jobs/interest/{iid}", dependencies=[Depends(check_csrf)])
def hr_update(request: Request, iid: int, status: str = Form(...), note: str = Form(""), told: str = Form(""),
              db: Session = Depends(get_db), user=Depends(require(*HR))):
    ii = db.get(InternalInterest, iid) or _404()
    try:
        J.hr_update(db, user, ii, status, note, bool(told))
        db.commit()
        flash(request, "Saved")
    except J.JobsError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/recruit/req/{ii.requisition_id}#internal")


@router.get("/jobs/interest/{iid}/move")
def start_move(request: Request, iid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    ii = db.get(InternalInterest, iid) or _404()
    req = ii.requisition
    q = {"type": J.suggested_event(db, ii), "to_org_unit": req.org_unit_code, "to_position": req.position_id or "",
         "to_job": req.job_code, "to_grade": req.grade_code or "", "interest": ii.id,
         "note": f"Internal job {req.ref}"}
    return redirect(f"/people/{ii.employee_id}/event?{urlencode(q)}")


@router.post("/jobs/req/{rid}", dependencies=[Depends(check_csrf)])
def req_settings(request: Request, rid: int, internal: str = Form(""), internal_until: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(require(*HR))):
    req = db.get(Requisition, rid) or _404()
    req.internal = bool(internal)
    until = parse_date(internal_until) if internal_until else None
    if until and until < date.today():
        flash(request, "The closing day is in the past", "bad")
        return redirect(f"/recruit/req/{rid}#internal")
    req.internal_until = until
    audit(db, user, "internal_board", "requisition", req.ref, f"internal={req.internal} until={until}")
    db.commit()
    flash(request, "Shown on the internal job board" if req.internal else "Removed from the internal job board")
    return redirect(f"/recruit/req/{rid}#internal")


def _404():
    raise HTTPException(404)
