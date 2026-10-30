from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..i18n import rule_text
from ..models import ApprovalRequest, Employee, LeaveRequest
from ..models.hrdev import LEAVE_PARTS
from ..security import check_csrf, current_user, require
from ..services import approvals
from ..services import leave as L
from ..services.common import normalize_employee_id, user_scope
from .ot import _d, team_for
from .templating import flash, lang_of, redirect, render

router = APIRouter(prefix="/leave")


def _visible(db: Session, user, emp_id: str, unit: str) -> bool:
    scope = user_scope(db, user)
    return scope.all or unit in scope.org_units or emp_id == user.employee_id


@router.get("")
def leave_list(request: Request, status: str = "", employee: str = "", d0: str = "", d1: str = "", db: Session = Depends(get_db), user=Depends(current_user)):
    start = _d(d0) or date(date.today().year, 1, 1)
    end = _d(d1) or date(date.today().year, 12, 31)
    q = select(LeaveRequest).where(LeaveRequest.start_date <= end, LeaveRequest.end_date >= start).order_by(LeaveRequest.start_date.desc())
    if status:
        q = q.where(LeaveRequest.status == status)
    if employee:
        q = q.where(LeaveRequest.employee_id == normalize_employee_id(employee))
    rows = [r for r in db.scalars(q).all() if _visible(db, user, r.employee_id, r.employee.org_unit_code)]
    me = db.get(Employee, user.employee_id) if user.employee_id else None
    mine = L.balances(db, me, date.today().year) if me else []
    return render(request, "leave/list.html", {"rows": rows, "status": status, "employee": employee, "d0": start, "d1": end, "mine": mine})


@router.get("/new")
def leave_new(request: Request, employee: str = "", db: Session = Depends(get_db), user=Depends(current_user)):
    team = team_for(db, user)
    return render(request, "leave/new.html", {"team": team, "types": L.types(db), "parts": LEAVE_PARTS,
                                              "employee": employee or (user.employee_id or ""), "today": date.today()})


def _read(form, db, user):
    team = {e.employee_id: e for e in team_for(db, user)}
    emp = team.get(normalize_employee_id(form.get("employee_id") or ""))
    d0, d1 = _d(form.get("start_date")), _d(form.get("end_date") or form.get("start_date"))
    part = form.get("part") if form.get("part") in LEAVE_PARTS else "full"
    return emp, form.get("leave_type") or "", d0, d1, part


@router.post("/check", dependencies=[Depends(check_csrf)], response_class=HTMLResponse)
async def leave_check(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    form = await request.form()
    emp, code, d0, d1, part = _read(form, db, user)
    lt = L.db_type(db, code)
    if emp is None or lt is None or d0 is None or d1 is None:
        return '<p class="note">…</p>'
    if d0 != d1:
        part = "full"
    lang = lang_of(request)
    findings, days = L.check(db, emp, lt, d0, d1, part, form.get("has_document") == "on")
    bal = next((b for b in L.balances(db, emp, d0.year) if b.type.code == lt.code), None)
    left = "" if bal is None or bal.left is None else f" · {rule_text(lang, {'code': 'leave_left', 'params': {'left': bal.left, 'year': d0.year}})}"
    ok = '<span class="tag ok">OK</span>' if not any(f.level == "block" for f in findings) else '<span class="tag bad">blocked</span>'
    items = "".join(f'<li class="{f.level}">{rule_text(lang, f.as_dict())}</li>' for f in findings)
    return f'<div><strong class="mono">{emp.employee_id}</strong> {ok} <span class="note">{days:g} d{left}</span><ul class="find">{items}</ul></div>'


@router.post("/new", dependencies=[Depends(check_csrf)])
async def leave_create(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    form = await request.form()
    emp, code, d0, d1, part = _read(form, db, user)
    if emp is None:
        flash(request, "Choose an employee from your team", "bad")
        return redirect("/leave/new")
    if d0 is None or d1 is None:
        flash(request, "Choose the dates", "bad")
        return redirect(f"/leave/new?employee={emp.employee_id}")
    res = L.create(db, user, emp, code, d0, d1, part, form.get("reason") or "", has_document=form.get("has_document") == "on")
    if not res.ok:
        db.rollback()
        lang = lang_of(request)
        for f in res.findings:
            if f.level == "block":
                flash(request, rule_text(lang, f.as_dict()), "bad")
        return redirect(f"/leave/new?employee={emp.employee_id}")
    db.commit()
    flash(request, f"Leave {res.request.ref} sent to the Department Head")
    return redirect(f"/leave/{res.request.id}")


@router.get("/balances")
def leave_balances(request: Request, year: int = 0, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "dept_head", "supervisor", "gm"))):
    year = year or date.today().year
    team = team_for(db, user)
    rows = [(e, {b.type.code: b for b in L.balances(db, e, year)}) for e in team]
    return render(request, "leave/balances.html", {"rows": rows, "types": L.types(db), "year": year})


@router.get("/{rid}")
def leave_detail(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(LeaveRequest, rid)
    if r is None or not _visible(db, user, r.employee_id, r.employee.org_unit_code):
        raise HTTPException(404)
    appr = db.get(ApprovalRequest, r.approval_id) if r.approval_id else None
    can_cancel = bool(appr and appr.status == "pending" and (user.id == appr.submitted_by_user_id or user.has("hr_manager")))
    bal = L.balances(db, r.employee, r.start_date.year)
    return render(request, "leave/detail.html", {"r": r, "appr": appr, "can": appr and approvals.can_decide(user, appr),
                                                 "can_cancel": can_cancel, "bal": bal, "parts": LEAVE_PARTS})


@router.post("/{rid}/cancel", dependencies=[Depends(check_csrf)])
def leave_cancel(request: Request, rid: int, reason: str = Form("cancelled"), db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(LeaveRequest, rid)
    if r is None or not r.approval_id:
        raise HTTPException(404)
    try:
        approvals.cancel(db, db.get(ApprovalRequest, r.approval_id), user, reason or "cancelled")
        db.commit()
        flash(request, f"Leave {r.ref} cancelled")
    except approvals.ApprovalError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/leave/{rid}")
