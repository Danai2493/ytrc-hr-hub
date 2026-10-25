from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PayBatch, PayTotal
from ..security import check_csrf, require
from ..services import paybatch as PB
from ..services.common import audit, get_setting
from ..services.timecalc import payroll_period
from .templating import flash, redirect, render

router = APIRouter(prefix="/pay")
HR = ("hr", "hr_manager")


@router.get("")
def pay_home(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    batches = db.scalars(select(PayBatch).order_by(PayBatch.period_start.desc(), PayBatch.id.desc()).limit(40)).all()
    today = date.today()
    cutoff = int(get_setting(db, "staff_ot_cutoff_day") or 20)
    suggestions = [("OPERATOR_HALF", *payroll_period("OPERATOR_HALF", today)), ("STAFF_MONTHLY", *payroll_period("STAFF_MONTHLY", today, cutoff))]
    totals = db.scalars(select(PayTotal).order_by(PayTotal.month.desc()).limit(40)).all()
    months = sorted({t.month for t in totals}, reverse=True)
    recon = PB.reconcile(db, months[0]) if months else []
    return render(request, "pay/home.html", {"batches": batches, "cycles": PB.CYCLES, "suggestions": suggestions, "totals": totals, "recon": recon, "recon_month": months[0] if months else None})


@router.post("/build", dependencies=[Depends(check_csrf)])
def build(request: Request, cycle: str = Form(...), start: str = Form(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    try:
        d = date.fromisoformat(start)
        p0, p1 = payroll_period(cycle, d, int(get_setting(db, "staff_ot_cutoff_day") or 20))
        b = PB.build(db, user, cycle, p0, p1)
        db.commit()
    except (ValueError, PB.BatchError) as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect("/pay")
    flash(request, f"Batch #{b.id} built: {b.totals['hours']}h for {b.totals['people']} people")
    return redirect(f"/pay/{b.id}")


@router.get("/{bid}")
def batch_detail(request: Request, bid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    b = db.get(PayBatch, bid)
    if b is None:
        raise HTTPException(404)
    return render(request, "pay/batch.html", {"b": b, "cycles": PB.CYCLES})


@router.post("/{bid}/rebuild", dependencies=[Depends(check_csrf)])
def rebuild(request: Request, bid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    b = db.get(PayBatch, bid)
    if b is None or b.status not in ("draft", "checked"):
        flash(request, "Only draft or checked batches can be rebuilt", "bad")
        return redirect(f"/pay/{bid}")
    PB.fill(db, b)
    audit(db, user, "rebuild", "pay_batch", b.id, "Rebuilt from verified OT")
    db.commit()
    flash(request, "Batch rebuilt from the latest verified OT. It needs a new check.")
    return redirect(f"/pay/{bid}")


@router.post("/{bid}/status", dependencies=[Depends(check_csrf)])
def set_status(request: Request, bid: int, status: str = Form(...), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require(*HR))):
    b = db.get(PayBatch, bid)
    try:
        PB.mark(db, user, b, status, note)
        db.commit()
        flash(request, f"Batch #{bid} is now {status}")
    except PB.BatchError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/pay/{bid}")


@router.get("/{bid}/export.csv")
def export(bid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    b = db.get(PayBatch, bid)
    if b is None:
        raise HTTPException(404)
    if b.status == "draft":
        raise HTTPException(400, "A second HR person must check the batch before export")
    text = PB.export_csv(db, b)
    audit(db, user, "export", "pay_batch", b.id, "Bplus file downloaded")
    db.commit()
    name = f"bplus_{b.cycle.lower()}_{b.period_start:%Y%m%d}_{b.period_end:%Y%m%d}_b{b.batch_no}.csv"
    return Response(text.encode("utf-8-sig"), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/totals", dependencies=[Depends(check_csrf)])
async def import_totals(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    n = PB.import_totals(db, user, (await file.read()).decode("utf-8-sig", errors="replace"))
    db.commit()
    flash(request, f"{n} rows of Bplus totals imported")
    return redirect("/pay")
