from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DataIssue
from ..security import check_csrf, require
from ..services import health as H
from ..services.common import audit, user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/health")


@router.get("")
def issues(request: Request, rule: str = "", status: str = "open", employee: str = "", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm", "dept_head"))):
    scope = user_scope(db, user)
    q = select(DataIssue).order_by(DataIssue.due_date, DataIssue.id)
    if status:
        q = q.where(DataIssue.status == status)
    if rule:
        q = q.where(DataIssue.rule_code == rule)
    if employee:
        q = q.where(DataIssue.employee_id == employee)
    rows = db.scalars(q).all()
    if not scope.all:
        rows = [i for i in rows if i.org_unit_code in scope.org_units]
    counts: dict[str, int] = {}
    for i in db.scalars(select(DataIssue).where(DataIssue.status == "open")):
        if scope.all or i.org_unit_code in scope.org_units:
            counts[i.rule_code] = counts.get(i.rule_code, 0) + 1
    return render(request, "health/list.html", {"rows": rows, "rules": H.RULES, "counts": counts, "rule": rule, "status": status, "employee": employee})


@router.post("/run", dependencies=[Depends(check_csrf)])
def run(request: Request, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    res = H.run(db)
    audit(db, user, "run", "data_health", None, str(res))
    db.commit()
    flash(request, f"Checks done: {res['added']} new, {res['closed']} closed, {res['open']} open")
    return redirect("/health")


@router.post("/{iid}", dependencies=[Depends(check_csrf)])
def resolve(request: Request, iid: int, action: str = Form(...), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "dept_head"))):
    i = db.get(DataIssue, iid)
    if i is None:
        return redirect("/health")
    if action == "ignore":
        if not note.strip():
            flash(request, "Say why the issue can be ignored", "bad")
            return redirect("/health")
        i.status = "ignored"
    elif action == "reopen":
        i.status = "open"
    i.resolved_by, i.resolved_at, i.resolution_note = user.label, datetime.now(), note[:300] or None
    audit(db, user, action, "data_issue", iid, f"{i.rule_code}: {note}")
    db.commit()
    return redirect("/health")
