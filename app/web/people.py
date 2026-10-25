from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    ApprovalRequest,
    DataIssue,
    Employee,
    Grade,
    Job,
    OrgUnit,
    OTRequest,
    Position,
    Shift,
)
from ..models.core import (
    EMPLOYEE_GROUPS,
    EVENT_TYPES,
    EXIT_REASONS,
    GENDERS,
    NATIONALITIES,
    PAY_TYPES,
    TEAMS,
    TITLES,
)
from ..security import check_csrf, client_ip, current_user, require
from ..services import people as P
from ..services.common import audit, next_free_employee_id, user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/people")
HR = ("hr", "hr_manager")


def _lists(db: Session) -> dict:
    return {
        "units": db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.sort_order, OrgUnit.code)).all(),
        "positions": db.scalars(select(Position).where(Position.active.is_(True)).order_by(Position.org_unit_code, Position.title)).all(),
        "jobs": db.scalars(select(Job).where(Job.active.is_(True)).order_by(Job.title_en)).all(),
        "grades": db.scalars(select(Grade).order_by(Grade.rank.desc())).all(),
        "shifts": db.scalars(select(Shift).order_by(Shift.code)).all(),
        "GROUPS": EMPLOYEE_GROUPS,
        "PAY_TYPES": PAY_TYPES,
        "NATIONALITIES": NATIONALITIES,
        "TITLES": TITLES,
        "GENDERS": GENDERS,
        "TEAMS": TEAMS,
        "EXIT_REASONS": EXIT_REASONS,
        "EVENT_TYPES": EVENT_TYPES,
    }


def _visible(db: Session, user, emp: Employee) -> bool:
    scope = user_scope(db, user)
    return scope.all or emp.org_unit_code in scope.org_units or emp.employee_id == user.employee_id


@router.get("")
def people_list(
    request: Request,
    q: str = "",
    group: str = "",
    unit: str = "",
    status: str = "Active",
    db: Session = Depends(get_db),
    user=Depends(require("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor")),
):
    scope = user_scope(db, user)
    stmt = select(Employee).order_by(Employee.employee_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Employee.employee_id.ilike(like), Employee.full_name_en.ilike(like), Employee.full_name_th.ilike(like), Employee.nickname.ilike(like)))
    if group:
        stmt = stmt.where(Employee.employee_group == group)
    if status:
        stmt = stmt.where(Employee.status == status)
    rows = db.scalars(stmt).all()
    if unit:
        from ..services.common import descendants

        allowed = descendants(db, [unit])
        rows = [e for e in rows if e.org_unit_code in allowed]
    if not scope.all:
        rows = [e for e in rows if e.org_unit_code in scope.org_units]
    open_issues = {}
    for i in db.scalars(select(DataIssue).where(DataIssue.status == "open", DataIssue.employee_id.is_not(None))):
        open_issues[i.employee_id] = open_issues.get(i.employee_id, 0) + 1
    return render(request, "people/list.html", {"rows": rows, "q": q, "group": group, "unit": unit, "status": status, "issues": open_issues, **_lists(db)})


@router.get("/export.csv")
def people_export(db: Session = Depends(get_db), user=Depends(require(*HR))):
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["employee_id", "employee_group", "pay_type", "title", "full_name_en", "full_name_th", "nationality", "birth_date", "join_date", "org_unit_code", "position_id", "job_code", "grade_code", "team", "status", "exit_date", "exit_reason_code"]
    w.writerow(cols)
    for e in db.scalars(select(Employee).order_by(Employee.employee_id)):
        w.writerow([getattr(e, c) or "" for c in cols])
    audit(db, user, "export", "employee", None, "Employee master CSV export")
    db.commit()
    return StreamingResponse(iter([buf.getvalue().encode("utf-8-sig")]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="employee_master_{date.today():%Y%m%d}.csv"'})


@router.get("/new")
def new_form(request: Request, group: str = "Operator", db: Session = Depends(get_db), user=Depends(require(*HR))):
    return render(request, "people/form.html", {"emp": None, "data": {"employee_group": group, "employee_id": next_free_employee_id(db, group), "pay_type": "Daily" if group == "Operator" else "Monthly", "nationality": "Thai"}, **_lists(db)})


@router.post("/new", dependencies=[Depends(check_csrf)])
async def new_submit(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    form = dict(await request.form())
    data = {k: form.get(k) for k in P.EDITABLE + ["employee_id"]}
    restricted = {k: form.get(k) for k in P.RESTRICTED}
    try:
        appr = P.request_create(db, user, data, restricted)
    except P.PeopleError as e:
        db.rollback()
        return render(request, "people/form.html", {"emp": None, "data": data, "error": str(e), **_lists(db)}, 400)
    db.commit()
    flash(request, f"New employee sent for a second HR check (request #{appr.id})")
    return redirect(f"/approvals/{appr.id}")


@router.get("/{emp_id}")
def detail(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    emp = db.get(Employee, emp_id)
    if emp is None or not _visible(db, user, emp):
        raise HTTPException(404, "Employee not found")
    ot = db.scalars(select(OTRequest).where(OTRequest.employee_id == emp_id).order_by(OTRequest.work_date.desc()).limit(15)).all()
    issues = db.scalars(select(DataIssue).where(DataIssue.employee_id == emp_id, DataIssue.status == "open")).all()
    reqs = db.scalars(select(ApprovalRequest).where(ApprovalRequest.employee_id == emp_id).order_by(ApprovalRequest.created_at.desc()).limit(10)).all()
    show_restricted = user.has("hr_manager", "hr") and request.query_params.get("restricted") == "1"
    if show_restricted:
        audit(db, user, "view_restricted", "employee", emp_id, "Viewed restricted data", ip=client_ip(request))
        db.commit()
    return render(request, "people/detail.html", {"emp": emp, "ot": ot, "issues": issues, "reqs": reqs, "show_restricted": show_restricted, "soft": P.soft_checks(db, emp), "unit_on": P.org_unit_on, **_lists(db)})


@router.get("/{emp_id}/edit")
def edit_form(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*HR))):
    emp = db.get(Employee, emp_id) or _404()
    data = {k: getattr(emp, k) for k in P.EDITABLE + ["employee_id"]}
    return render(request, "people/form.html", {"emp": emp, "data": data, **_lists(db)})


@router.post("/{emp_id}/edit", dependencies=[Depends(check_csrf)])
async def edit_submit(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*HR))):
    emp = db.get(Employee, emp_id) or _404()
    form = dict(await request.form())
    data = {k: form.get(k) for k in P.EDITABLE if k in form and k not in P.EVENT_FIELDS}
    try:
        appr = P.request_update(db, user, emp, data, form.get("note", ""))
    except P.PeopleError as e:
        db.rollback()
        return render(request, "people/form.html", {"emp": emp, "data": {**{k: getattr(emp, k) for k in P.EDITABLE + ['employee_id']}, **data}, "error": str(e), **_lists(db)}, 400)
    db.commit()
    flash(request, f"Change sent for a second HR check (request #{appr.id})")
    return redirect(f"/approvals/{appr.id}")


@router.get("/{emp_id}/event")
def event_form(request: Request, emp_id: str, type: str = "transfer", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "dept_head"))):
    emp = db.get(Employee, emp_id) or _404()
    if not _visible(db, user, emp):
        _404()
    return render(request, "people/event.html", {"emp": emp, "etype": type, "steps": P.EVENT_STEPS, **_lists(db)})


@router.post("/{emp_id}/event", dependencies=[Depends(check_csrf)])
async def event_submit(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "dept_head"))):
    emp = db.get(Employee, emp_id) or _404()
    if not _visible(db, user, emp):
        _404()
    form = dict(await request.form())
    etype = form.get("event_type", "")
    data = {k: form.get(k) or None for k in ("effective_date", "to_org_unit", "to_position", "to_job", "to_grade", "to_group", "reason_code", "note")}
    try:
        appr = P.request_event(db, user, emp, etype, data)
    except P.PeopleError as e:
        db.rollback()
        return render(request, "people/event.html", {"emp": emp, "etype": etype, "error": str(e), "data": data, "steps": P.EVENT_STEPS, **_lists(db)}, 400)
    db.commit()
    flash(request, f"{etype.replace('_', ' ').title()} sent for approval (request #{appr.id})")
    return redirect(f"/approvals/{appr.id}")


@router.post("/{emp_id}/restricted", dependencies=[Depends(check_csrf)])
async def restricted_submit(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*HR))):
    from ..models import EmployeeRestricted

    emp = db.get(Employee, emp_id) or _404()
    form = dict(await request.form())
    r = emp.restricted or EmployeeRestricted(employee_id=emp_id)
    before = {k: str(getattr(r, k) or "") for k in P.RESTRICTED}
    try:
        vals = P.clean({k: form.get(k) for k in P.RESTRICTED})
    except P.PeopleError as e:
        flash(request, str(e), "bad")
        return redirect(f"/people/{emp_id}?restricted=1")
    for k, v in vals.items():
        setattr(r, k, v)
    db.add(r)
    after = {k: str(getattr(r, k) or "") for k in P.RESTRICTED}
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    # Values are not written to the log, only which fields changed.
    audit(db, user, "update_restricted", "employee", emp_id, "Restricted fields: " + ", ".join(changed), {"fields": list(changed)}, ip=client_ip(request))
    db.commit()
    flash(request, "Restricted data saved")
    return redirect(f"/people/{emp_id}?restricted=1")


def _404():
    raise HTTPException(404, "Not found")
