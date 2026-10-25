from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApprovalRequest, DataIssue, OTRequest
from ..security import current_user
from ..services import analytics as A
from ..services import approvals
from ..services.common import user_scope
from ..services.timecalc import payroll_period
from .templating import render

router = APIRouter()


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    scope = user_scope(db, user)
    today = date.today()
    p0, p1 = payroll_period("OPERATOR_HALF", today)
    s0, s1 = payroll_period("STAFF_MONTHLY", today)
    inbox = approvals.inbox(db, user)
    my_ot = []
    if user.employee_id:
        my_ot = db.scalars(
            select(OTRequest).where(OTRequest.employee_id == user.employee_id).order_by(OTRequest.work_date.desc()).limit(8)
        ).all()
    mine = db.scalars(
        select(ApprovalRequest).where(ApprovalRequest.submitted_by_user_id == user.id).order_by(ApprovalRequest.created_at.desc()).limit(8)
    ).all()
    ctx = {
        "inbox": inbox,
        "mine": mine,
        "my_ot": my_ot,
        "scope": scope,
        "staff_period": (s0, s1),
        "op_period": (p0, p1),
    }
    if user.has("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor"):
        ctx["hc"] = A.headcount(db, scope)
        ctx["ot_staff"] = A.ot_summary(db, scope, s0, s1)
        ctx["ot_ops"] = A.ot_summary(db, scope, p0, p1)
        ctx["weeks"] = A.ot_by_week(db, scope, 8)
        ctx["issues"] = A.open_issue_count(db, scope)
        ctx["queue"] = A.review_queue_count(db) if user.has("hr", "hr_manager") else None
        ctx["overdue"] = db.scalars(
            select(DataIssue).where(DataIssue.status == "open", DataIssue.due_date < today).limit(200)
        ).all()
    return render(request, "home.html", ctx)
