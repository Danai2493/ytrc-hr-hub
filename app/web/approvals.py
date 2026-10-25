from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApprovalRequest, AuditLog, Employee, OTRequest
from ..security import check_csrf, current_user
from ..services import approvals as AP
from ..services.common import user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/approvals")


@router.get("")
def inbox(request: Request, view: str = "inbox", db: Session = Depends(get_db), user=Depends(current_user)):
    if view == "mine":
        rows = db.scalars(select(ApprovalRequest).where(ApprovalRequest.submitted_by_user_id == user.id).order_by(ApprovalRequest.created_at.desc()).limit(200)).all()
    elif view == "all" and user.has("hr", "hr_manager", "gm", "auditor"):
        rows = db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(300)).all()
    else:
        view = "inbox"
        rows = AP.inbox(db, user)
    ot = {}
    for r in rows:
        if r.request_type == "OT":
            ot[r.id] = db.get(OTRequest, int(r.entity_id))
    return render(request, "approvals/list.html", {"rows": rows, "view": view, "ot": ot})


@router.post("/bulk", dependencies=[Depends(check_csrf)])
async def bulk(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    form = await request.form()
    decision = form.get("decision")
    comment = form.get("comment") or ""
    ids = [int(x) for x in form.getlist("ids") if x.isdigit()]
    ok, bad = 0, []
    for rid in ids:
        r = db.get(ApprovalRequest, rid)
        try:
            AP.decide(db, r, user, decision, comment)
            ok += 1
        except AP.ApprovalError as e:
            bad.append(f"#{rid}: {e}")
    db.commit()
    flash(request, f"{ok} request(s) {decision}")
    for b in bad[:10]:
        flash(request, b, "bad")
    return redirect("/approvals")


@router.get("/{rid}")
def detail(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(ApprovalRequest, rid)
    if r is None:
        raise HTTPException(404)
    scope = user_scope(db, user)
    involved = r.submitted_by_user_id == user.id or any(s.approver_employee_id == user.employee_id for s in r.steps if user.employee_id)
    if not (scope.all or involved or (r.org_unit_code and r.org_unit_code in scope.org_units) or AP.can_decide(user, r)):
        raise HTTPException(404)
    emp = db.get(Employee, r.employee_id) if r.employee_id else None
    ot = db.get(OTRequest, int(r.entity_id)) if r.request_type == "OT" else None
    log = db.scalars(select(AuditLog).where(AuditLog.entity == "approval", AuditLog.entity_id == str(rid)).order_by(AuditLog.at)).all()
    approver_names = {}
    for s in r.steps:
        if s.approver_employee_id:
            e = db.get(Employee, s.approver_employee_id)
            approver_names[s.approver_employee_id] = e.full_name_en if e else ""
    return render(request, "approvals/detail.html", {"r": r, "emp": emp, "ot": ot, "can": AP.can_decide(user, r), "log": log, "names": approver_names,
                                                       "can_cancel": r.status == "pending" and (r.submitted_by_user_id == user.id or user.has("hr_manager"))})


@router.post("/{rid}/decide", dependencies=[Depends(check_csrf)])
def decide(request: Request, rid: int, decision: str = Form(...), comment: str = Form(""), db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(ApprovalRequest, rid)
    if r is None:
        raise HTTPException(404)
    try:
        AP.decide(db, r, user, decision, comment)
        db.commit()
        flash(request, f"Request #{rid} {decision}")
    except Exception as e:  # ApprovalError or a check that fails when the change is applied
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/approvals/{rid}")


@router.post("/{rid}/cancel", dependencies=[Depends(check_csrf)])
def cancel(request: Request, rid: int, reason: str = Form(...), db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(ApprovalRequest, rid)
    try:
        AP.cancel(db, r, user, reason)
        db.commit()
        flash(request, f"Request #{rid} cancelled")
    except AP.ApprovalError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/approvals/{rid}")
