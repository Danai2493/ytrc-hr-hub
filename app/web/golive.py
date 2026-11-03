"""Go-live: load the real Employee Master workbook and opening leave balances, with a readiness checklist."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import check_csrf, require
from ..services import golive as G
from .templating import flash, redirect, render

router = APIRouter(prefix="/admin/golive")
HR = ("hr_manager", "hr")
MAX_BYTES = 10 * 1024 * 1024


@router.get("")
def home(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    return render(request, "admin/golive.html", {"ready": G.readiness(db), "year": date.today().year, "is_hrm": user.has("hr_manager")})


async def _read(file: UploadFile) -> bytes:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise G.ImportError_("The file is larger than 10 MB")
    return data


@router.post("/master", dependencies=[Depends(check_csrf)])
async def master_preview(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    try:
        master, restricted = G.read_workbook(await _read(file))
        plan = G.plan_master(db, master, restricted)
    except G.ImportError_ as e:
        flash(request, str(e), "bad")
        return redirect("/admin/golive")
    token = G.save_pending("master", plan.to_json()) if plan.counts["new"] + plan.counts["update"] else ""
    return render(request, "admin/golive_master.html", {"plan": plan, "counts": plan.counts, "token": token, "filename": file.filename,
                                                        "is_hrm": user.has("hr_manager")})


@router.post("/master/apply", dependencies=[Depends(check_csrf)])
def master_apply(request: Request, token: str = Form(...), db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    try:
        res = G.apply_master(db, user, G.load_pending("master", token))
        db.commit()
        flash(request, f"Employee Master loaded | {res['new']} new, {res['updated']} updated")
    except G.ImportError_ as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect("/admin/golive")


@router.get("/opening-template")
def opening_template(year: int = 0, db: Session = Depends(get_db), user=Depends(require(*HR))):
    year = year or date.today().year
    return Response(G.opening_template(db, year), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Leave_opening_{year}.xlsx"', "Cache-Control": "no-store"})


@router.post("/opening", dependencies=[Depends(check_csrf)])
async def opening_preview(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    try:
        rows = G.plan_opening(db, await _read(file))
    except G.ImportError_ as e:
        flash(request, str(e), "bad")
        return redirect("/admin/golive")
    ok = [r for r in rows if not r["errors"]]
    token = G.save_pending("opening", ok) if ok else ""
    return render(request, "admin/golive_opening.html", {"rows": rows, "ok": len(ok), "bad": len(rows) - len(ok), "token": token,
                                                         "filename": file.filename})


@router.post("/opening/apply", dependencies=[Depends(check_csrf)])
def opening_apply(request: Request, token: str = Form(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    try:
        n = G.apply_opening(db, user, G.load_pending("opening", token))
        db.commit()
        flash(request, f"Opening balances loaded | {n}")
    except G.ImportError_ as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect("/admin/golive")
