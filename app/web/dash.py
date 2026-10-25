from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models.core import EXIT_REASONS
from ..security import require
from ..services import analytics as A
from ..services.common import user_scope
from ..services.timecalc import payroll_period
from .templating import render

router = APIRouter(prefix="/dash")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "auditor")


def _range(d0: str, d1: str, default_days: int = 30) -> tuple[date, date]:
    try:
        a = date.fromisoformat(d0) if d0 else date.today() - timedelta(days=default_days)
        b = date.fromisoformat(d1) if d1 else date.today()
    except ValueError:
        a, b = date.today() - timedelta(days=default_days), date.today()
    return a, b


@router.get("")
def people_dash(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    scope = user_scope(db, user)
    mv = A.movement(db, scope, 12)
    year_ago = date.today().replace(day=1) - timedelta(days=365)
    return render(request, "dash/people.html", {"tab": "people", "hc": A.headcount(db, scope), "mv": mv, "reasons": A.exit_reasons(db, scope, year_ago), "reason_names": dict(EXIT_REASONS)})


@router.get("/ot")
def ot_dash(request: Request, d0: str = "", d1: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    scope = user_scope(db, user)
    if not d0:
        a, b = payroll_period("STAFF_MONTHLY", date.today())
    else:
        a, b = _range(d0, d1)
    return render(request, "dash/ot.html", {"tab": "ot", "d0": a, "d1": b, "s": A.ot_summary(db, scope, a, b), "months": A.ot_by_month(db, scope, 6), "weeks": A.ot_by_week(db, scope, 10)})


@router.get("/attendance")
def att_dash(request: Request, d0: str = "", d1: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    scope = user_scope(db, user)
    a, b = _range(d0, d1, 30)
    return render(request, "dash/attendance.html", {"tab": "attendance", "d0": a, "d1": b, "ab": A.absence(db, scope, a, b)})


@router.get("/kpi")
def kpi_pack(request: Request, month: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    """Monthly HR KPI pack: one printable page for management."""
    scope = user_scope(db, user)
    try:
        m0 = date.fromisoformat(month + "-01") if month else A.add_months(date.today().replace(day=1), -1)
    except ValueError:
        m0 = A.add_months(date.today().replace(day=1), -1)
    m1 = A.add_months(m0, 1) - timedelta(days=1)
    mv = A.movement(db, scope, 12, today=m1)
    from sqlalchemy import func, select

    from ..models import DataIssue

    issues_open = db.scalar(select(func.count()).select_from(DataIssue).where(DataIssue.status == "open")) or 0
    issues_overdue = db.scalar(select(func.count()).select_from(DataIssue).where(DataIssue.status == "open", DataIssue.due_date < date.today())) or 0
    return render(request, "dash/kpi.html", {
        "tab": "kpi", "m0": m0, "m1": m1, "hc": A.headcount(db, scope, m1), "mv": mv,
        "ot": A.ot_summary(db, scope, m0, m1), "ab": A.absence(db, scope, m0, m1),
        "reasons": A.exit_reasons(db, scope, m0, m1), "reason_names": dict(EXIT_REASONS),
        "issues_open": issues_open, "issues_overdue": issues_overdue,
    })
