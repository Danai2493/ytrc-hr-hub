from __future__ import annotations

from datetime import date, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..i18n import rule_text
from ..models import ApprovalRequest, DailyAttendance, Employee, OTRequest
from ..security import check_csrf, current_user, require
from ..services import approvals
from ..services import ot as OT
from ..services.common import normalize_employee_id, user_scope
from ..services.timecalc import payroll_period
from .templating import flash, lang_of, redirect, render

router = APIRouter(prefix="/ot")


def _t(s: str):
    try:
        hh, mm = s.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _d(s: str):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def team_for(db: Session, user) -> list[Employee]:
    scope = user_scope(db, user)
    q = select(Employee).where(Employee.status == "Active").order_by(Employee.employee_id)
    rows = db.scalars(q).all()
    if scope.all:
        return list(rows)
    team = [e for e in rows if e.org_unit_code in scope.org_units]
    if user.employee_id and not any(e.employee_id == user.employee_id for e in team):
        me = db.get(Employee, user.employee_id)
        if me:
            team.append(me)
    return team


@router.get("")
def ot_list(request: Request, status: str = "", employee: str = "", d0: str = "", d1: str = "", db: Session = Depends(get_db), user=Depends(current_user)):
    scope = user_scope(db, user)
    start = _d(d0) or (date.today() - timedelta(days=31))
    end = _d(d1) or (date.today() + timedelta(days=31))
    q = select(OTRequest).where(OTRequest.work_date >= start, OTRequest.work_date <= end).order_by(OTRequest.work_date.desc(), OTRequest.employee_id)
    if status:
        q = q.where(OTRequest.status == status)
    if employee:
        q = q.where(OTRequest.employee_id == normalize_employee_id(employee))
    rows = db.scalars(q).all()
    if not scope.all:
        rows = [r for r in rows if r.employee.org_unit_code in scope.org_units or r.employee_id == user.employee_id]
    return render(request, "ot/list.html", {"rows": rows, "status": status, "employee": employee, "d0": start, "d1": end})


@router.get("/new")
def ot_new(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    return render(request, "ot/new.html", {"team": team_for(db, user), "default_date": date.today()})


@router.post("/check", dependencies=[Depends(check_csrf)], response_class=HTMLResponse)
async def ot_check(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    """Live check while the supervisor fills the form."""
    form = await request.form()
    d = _d(form.get("work_date"))
    ids = form.getlist("employee_id")
    froms, tos = form.getlist("ot_from"), form.getlist("ot_to")
    lang = lang_of(request)
    team = {e.employee_id for e in team_for(db, user)}
    out = []
    for i, eid in enumerate(ids):
        eid = normalize_employee_id(eid)
        if not eid:
            continue
        t0, t1 = _t(froms[i] if i < len(froms) else ""), _t(tos[i] if i < len(tos) else "")
        if not (d and t0 and t1):
            continue
        emp = db.get(Employee, eid) if eid in team else None
        res = OT.preview(db, emp, d, t0, t1)
        items = "".join(f'<li class="{f.level}">{rule_text(lang, f.as_dict())}</li>' for f in res.findings if f.code != "after_the_fact")
        split = ", ".join(f"{k} {v / 60:g}h" for k, v in res.split.items())
        ok = '<span class="tag ok">OK</span>' if res.ok else '<span class="tag bad">blocked</span>'
        out.append(f'<div><strong class="mono">{eid}</strong> {ok} <span class="note">{res.day_kind} · {split}</span><ul class="find">{items}</ul></div>')
    return "".join(out) or '<p class="note">…</p>'


@router.post("/new", dependencies=[Depends(check_csrf)])
async def ot_create(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    form = await request.form()
    d = _d(form.get("work_date"))
    reason = form.get("reason", "")
    override = (form.get("override_note") or "").strip() or None
    team = {e.employee_id: e for e in team_for(db, user)}
    ids, froms, tos = form.getlist("employee_id"), form.getlist("ot_from"), form.getlist("ot_to")
    made, problems = [], []
    lang = lang_of(request)
    if d is None:
        flash(request, "Choose the work date", "bad")
        return redirect("/ot/new")
    for i, raw in enumerate(ids):
        eid = normalize_employee_id(raw)
        if not eid:
            continue
        emp = team.get(eid)
        t0, t1 = _t(froms[i] if i < len(froms) else ""), _t(tos[i] if i < len(tos) else "")
        if emp is None:
            problems.append(f"{eid}: not in your team")
            continue
        if not (t0 and t1):
            problems.append(f"{eid}: OT from/to missing")
            continue
        res = OT.create(db, user, emp, d, t0, t1, reason, override_note=override)
        if res.ok:
            made.append(res.request.ref)
        else:
            problems.append(f"{eid}: " + "; ".join(rule_text(lang, b.as_dict()) for b in res.blocks))
    db.commit()
    if made:
        flash(request, f"{len(made)} OT request(s) sent to the Department Head: {', '.join(made)}")
    for p in problems:
        flash(request, p, "bad")
    return redirect("/ot" if made and not problems else "/ot/new")


@router.get("/verify")
def ot_verify_page(request: Request, cycle: str = "OPERATOR_HALF", d0: str = "", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    start = _d(d0) or payroll_period(cycle, date.today())[0]
    p0, p1 = payroll_period(cycle, start)
    rows = db.scalars(
        select(OTRequest).where(OTRequest.status == "approved", OTRequest.work_date >= p0, OTRequest.work_date <= p1).order_by(OTRequest.work_date, OTRequest.employee_id)
    ).all()
    rows = [r for r in rows if (r.employee.employee_group == "Operator") == (cycle == "OPERATOR_HALF")]
    approvable = {}
    for r in rows:
        appr = db.get(ApprovalRequest, r.approval_id) if r.approval_id else None
        approvable[r.id] = bool(appr and approvals.can_decide(user, appr))
    return render(request, "ot/verify.html", {"rows": rows, "cycle": cycle, "p0": p0, "p1": p1, "approvable": approvable})


@router.post("/verify", dependencies=[Depends(check_csrf)])
async def ot_verify(request: Request, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if x.isdigit()]
    done, errors = OT.verify_many(db, user, ids)
    db.commit()
    flash(request, f"{done} OT line(s) verified")
    for e in errors[:10]:
        flash(request, e, "bad")
    return redirect(f"/ot/verify?cycle={form.get('cycle', 'OPERATOR_HALF')}&d0={form.get('d0', '')}")


@router.get("/{rid}")
def ot_detail(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    r = db.get(OTRequest, rid)
    if r is None:
        raise HTTPException(404)
    scope = user_scope(db, user)
    if not (scope.all or r.employee.org_unit_code in scope.org_units or r.employee_id == user.employee_id):
        raise HTTPException(404)
    att = db.scalar(select(DailyAttendance).where(DailyAttendance.employee_id == r.employee_id, DailyAttendance.work_date == r.work_date))
    appr = db.get(ApprovalRequest, r.approval_id) if r.approval_id else None
    return render(request, "ot/detail.html", {"r": r, "att": att, "appr": appr, "can": appr and approvals.can_decide(user, appr)})
