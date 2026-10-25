from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DailyAttendance, ScanImport
from ..security import check_csrf, require
from ..services import facescan
from ..services.common import descendants, user_scope
from .templating import flash, redirect, render

router = APIRouter(prefix="/attendance")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "supervisor")


@router.get("")
def day_view(request: Request, d: str = "", unit: str = "", only: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    try:
        day = date.fromisoformat(d) if d else date.today() - timedelta(days=1)
    except ValueError:
        day = date.today() - timedelta(days=1)
    scope = user_scope(db, user)
    rows = db.scalars(select(DailyAttendance).where(DailyAttendance.work_date == day).order_by(DailyAttendance.employee_id)).all()
    if not scope.all:
        rows = [a for a in rows if a.employee.org_unit_code in scope.org_units]
    if unit:
        allowed = descendants(db, [unit])
        rows = [a for a in rows if a.employee.org_unit_code in allowed]
    if only == "absent":
        rows = [a for a in rows if a.day_code == "a"]
    elif only == "late":
        rows = [a for a in rows if a.late_minutes > 5]
    elif only == "ot":
        rows = [a for a in rows if a.scan_ot_minutes > 30]
    imports = db.scalars(select(ScanImport).order_by(ScanImport.created_at.desc()).limit(8)).all()
    from ..models import OrgUnit

    units = db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.sort_order, OrgUnit.code)).all()
    summary = {
        "worked": sum(1 for a in rows if a.day_code == "1"),
        "absent": sum(1 for a in rows if a.day_code == "a"),
        "late": sum(1 for a in rows if a.late_minutes > 5),
        "ot": sum(1 for a in rows if a.scan_ot_minutes > 30),
    }
    return render(request, "attendance/day.html", {"rows": rows, "day": day, "unit": unit, "only": only, "imports": imports, "units": units, "summary": summary})


@router.post("/import", dependencies=[Depends(check_csrf)])
async def do_import(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager"))):
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        flash(request, "File too large (max 20 MB)", "bad")
        return redirect("/attendance")
    text = raw.decode("utf-8", errors="replace")
    imp = facescan.import_scans(db, file.filename or "upload.txt", text, user)
    db.commit()
    flash(request, f"{imp.lines_new} new scans from {imp.filename} ({imp.lines_total - imp.lines_new} already imported). Unknown scan IDs: {len([u for u in imp.unknown_users or [] if not u.startswith('unreadable')])}")
    return redirect(f"/attendance?d={imp.date_to or ''}")
