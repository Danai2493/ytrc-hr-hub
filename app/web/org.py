from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, Grade, Job, OrgUnit, Position, Shift
from ..models.core import ORG_LEVELS
from ..security import check_csrf, require
from ..services.common import audit, next_number
from .templating import flash, redirect, render

router = APIRouter(prefix="/org")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "auditor")


@router.get("")
def org_home(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    roots = db.scalars(select(OrgUnit).where(OrgUnit.parent_code.is_(None)).order_by(OrgUnit.sort_order)).all()
    counts = dict(db.execute(select(Employee.org_unit_code, func.count()).where(Employee.status == "Active").group_by(Employee.org_unit_code)).all())
    positions = db.scalars(select(Position).where(Position.active.is_(True)).order_by(Position.org_unit_code, Position.position_id)).all()
    seats = sum(p.seats for p in positions)
    filled = sum(min(p.filled, p.seats) for p in positions)
    return render(request, "org/home.html", {"roots": roots, "counts": counts, "positions": positions, "seats": seats, "filled": filled})


@router.get("/unit/{code}")
def unit_form(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    unit = db.get(OrgUnit, code) if code != "new" else None
    return render(request, "org/unit.html", {"unit": unit, "units": db.scalars(select(OrgUnit).order_by(OrgUnit.code)).all(), "levels": ORG_LEVELS, "shifts": db.scalars(select(Shift)).all(), "heads": db.scalars(select(Employee).where(Employee.status == "Active", Employee.employee_group.in_(("Manager", "Staff", "Japanese Management"))).order_by(Employee.employee_id)).all()})


@router.post("/unit/{code}", dependencies=[Depends(check_csrf)])
async def unit_save(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    f = dict(await request.form())
    if code == "new":
        code = (f.get("code") or "").strip().upper()
        if not code or db.get(OrgUnit, code):
            flash(request, "Code missing or already used", "bad")
            return redirect("/org/unit/new")
        unit = OrgUnit(code=code)
        db.add(unit)
        before = {}
    else:
        unit = db.get(OrgUnit, code)
        if unit is None:
            raise HTTPException(404)
        before = {k: getattr(unit, k) for k in ("name_en", "name_th", "level", "parent_code", "head_employee_id", "calendar_code", "default_shift_code", "active")}
    unit.name_en = f.get("name_en") or code
    unit.name_th = f.get("name_th") or None
    unit.level = f.get("level") or "section"
    unit.parent_code = f.get("parent_code") or None
    unit.head_employee_id = f.get("head_employee_id") or None
    unit.calendar_code = f.get("calendar_code") or "OFFICE"
    unit.default_shift_code = f.get("default_shift_code") or None
    unit.active = f.get("active") == "on"
    if unit.parent_code == unit.code:
        flash(request, "A unit cannot be its own parent", "bad")
        db.rollback()
        return redirect(f"/org/unit/{code}")
    after = {k: getattr(unit, k) for k in before} if before else {}
    audit(db, user, "update" if before else "create", "org_unit", code, unit.name_en, {k: [before[k], after[k]] for k in before if before[k] != after[k]})
    db.commit()
    flash(request, "Org Unit saved")
    return redirect("/org")


@router.get("/position/{pid}")
def position_form(request: Request, pid: str, unit: str = "", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    pos = db.get(Position, pid) if pid != "new" else None
    return render(request, "org/position.html", {
        "pos": pos, "unit": unit,
        "units": db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.code)).all(),
        "jobs": db.scalars(select(Job).order_by(Job.title_en)).all(),
        "grades": db.scalars(select(Grade).order_by(Grade.rank.desc())).all(),
        "all_positions": db.scalars(select(Position).order_by(Position.position_id)).all(),
    })


@router.post("/position/{pid}", dependencies=[Depends(check_csrf)])
async def position_save(request: Request, pid: str, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    f = dict(await request.form())
    if pid == "new":
        pid = f"POS-{next_number(db, 'POS'):04d}"
        while db.get(Position, pid):
            pid = f"POS-{next_number(db, 'POS'):04d}"
        pos = Position(position_id=pid)
        db.add(pos)
    else:
        pos = db.get(Position, pid)
        if pos is None:
            raise HTTPException(404)
    pos.title = f.get("title") or "Position"
    pos.org_unit_code = f.get("org_unit_code")
    pos.job_code = f.get("job_code") or None
    pos.grade_code = f.get("grade_code") or None
    pos.seats = max(1, int(f.get("seats") or 1))
    pos.budgeted = f.get("budgeted") == "on"
    pos.budget_year = int(f["budget_year"]) if f.get("budget_year") else None
    pos.critical = f.get("critical") == "on"
    pos.min_backups_uo = int(f.get("min_backups_uo") or 0)
    pos.frozen = f.get("frozen") == "on"
    pos.reports_to_position_id = f.get("reports_to_position_id") or None
    pos.active = f.get("active", "on") == "on"
    audit(db, user, "save", "position", pid, pos.title)
    db.commit()
    flash(request, f"Position {pid} saved")
    return redirect("/org#positions")
