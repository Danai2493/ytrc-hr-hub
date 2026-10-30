from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import CaptureLine, Employee, FormCapture
from ..models.system import CAPTURE_TYPES
from ..security import check_csrf, client_ip, require
from ..services.ai import capture as C
from ..services.ai.providers import AIError
from ..services.common import audit
from .templating import flash, redirect, render

router = APIRouter(prefix="/capture")
WHO = ("hr", "hr_manager", "supervisor")
HR = ("hr", "hr_manager")


def _get(db: Session, cid: int, user) -> FormCapture:
    cap = db.get(FormCapture, cid)
    if cap is None:
        raise HTTPException(404)
    if not user.has(*HR) and cap.uploaded_by != user.label:
        raise HTTPException(404)
    return cap


@router.get("")
def queue(request: Request, status: str = "open", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    q = select(FormCapture).order_by(FormCapture.created_at.desc()).limit(200)
    if status == "open":
        q = q.where(FormCapture.status.in_(("needs_review", "extracted", "failed", "uploaded")))
    rows = db.scalars(q).all()
    if not user.has(*HR):
        rows = [r for r in rows if r.uploaded_by == user.label]
    return render(request, "capture/queue.html", {"rows": rows, "status": status, "types": CAPTURE_TYPES, "provider": settings.ai_provider})


@router.get("/new")
def new(request: Request, type: str = "OT_FORM", employee: str = "", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    return render(request, "capture/new.html", {"ftype": type, "employee": employee, "types": CAPTURE_TYPES, "provider": settings.ai_provider})


@router.post("/new", dependencies=[Depends(check_csrf)])
async def upload(request: Request, form_type: str = Form(...), employee: str = Form(""), file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(require(*WHO))):
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        flash(request, "Photo too large (max 15 MB)", "bad")
        return redirect("/capture/new")
    hint = {}
    if employee:
        emp = db.get(Employee, employee)
        if emp:
            hint["name"] = emp.full_name_en
            hint["employee_id"] = emp.employee_id
    try:
        cap = C.upload(db, user, form_type, data, file.content_type or "image/jpeg", file.filename or "photo.jpg", hint)
        if employee:
            cap.employee_id = employee
            if form_type == "WORK_PERMIT":
                C.validate_permit(db, cap)
            elif form_type == "LEAVE_FORM" and not (cap.header or {}).get("employee_id"):
                cap.header = {**(cap.header or {}), "employee_id": employee}
                C.validate_leave(db, cap)
        db.commit()
    except AIError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect("/capture/new")
    if cap.status == "failed":
        flash(request, f"AI could not read the photo: {cap.error}", "bad")
    elif cap.status == "extracted":
        flash(request, "AI read the form and every line passed the checks. Please confirm.")
    else:
        flash(request, "AI read the form. Some lines need a look.", "warn")
    return redirect(f"/capture/{cap.id}")


@router.get("/{cid}")
def review(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    cap = _get(db, cid, user)
    from ..services import leave as leave_service

    return render(request, "capture/review.html", {"cap": cap, "types": CAPTURE_TYPES, "thr": settings.ai_review_threshold, "is_hr": user.has(*HR),
                                                   "leave_types": leave_service.types(db) if cap.form_type == "LEAVE_FORM" else []})


@router.get("/{cid}/image")
def image(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    cap = _get(db, cid, user)
    if cap.form_type == "WORK_PERMIT":
        audit(db, user, "view_restricted", "form_capture", cid, "Viewed work permit photo", ip=client_ip(request))
        db.commit()
    return FileResponse(C.file_path(cap), media_type=cap.mime, headers={"Cache-Control": "private, no-store"})


@router.post("/{cid}/header", dependencies=[Depends(check_csrf)])
async def set_header(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    cap = _get(db, cid, user)
    f = await request.form()
    h = dict(cap.header or {})
    conf = dict(cap.header_confidence or {})
    for k in ("work_date", "department"):
        if f.get(k) is not None:
            if str(h.get(k) or "") != f.get(k):
                conf[k] = 1.0
            h[k] = f.get(k)
    if cap.form_type == "LEAVE_FORM":
        C.update_leave(db, user, cap, {k: f.get(k) for k in ("employee_id", "leave_type", "start_date", "end_date", "part", "reason")}
                       | {"medical_certificate": f.get("medical_certificate") == "on", "approved_by_signed": f.get("approved_by_signed") == "on"})
        db.commit()
        return redirect(f"/capture/{cid}")
    h["approved_by_signed"] = f.get("approved_by_signed") == "on"
    cap.header, cap.header_confidence = h, conf
    C.validate_ot(db, cap)
    audit(db, user, "edit", "form_capture", cid, "Header corrected")
    db.commit()
    return redirect(f"/capture/{cid}")


@router.post("/{cid}/line/{lid}", dependencies=[Depends(check_csrf)])
async def edit_line(request: Request, cid: int, lid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    cap = _get(db, cid, user)
    line = db.get(CaptureLine, lid)
    if line is None or line.capture_id != cid:
        raise HTTPException(404)
    f = await request.form()
    if f.get("drop"):
        C.drop_line(db, user, cap, line, f.get("note") or "not OT")
    else:
        changed = {k: f.get(k) for k in ("employee_id", "ot_from", "ot_to", "hours") if f.get(k) is not None and str(f.get(k)) != str(line.fields.get(k) or "")}
        if changed:
            C.update_line(db, user, cap, line, changed)
    db.commit()
    return redirect(f"/capture/{cid}#line-{line.line_no}")


@router.post("/{cid}/accept", dependencies=[Depends(check_csrf)])
async def accept(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    cap = _get(db, cid, user)
    f = await request.form()
    try:
        if cap.form_type == "OT_FORM":
            ids = [int(x) for x in f.getlist("line_ids") if x.isdigit()]
            made, errors = C.accept_ot(db, user, cap, ids, f.get("paper_signed") == "on", f.get("reason") or "")
            db.commit()
            flash(request, f"{made} OT request(s) created from the paper form")
            for e in errors:
                flash(request, e, "bad")
        elif cap.form_type == "LEAVE_FORM":
            req = C.accept_leave(db, user, cap, f.get("paper_signed") == "on")
            db.commit()
            flash(request, f"Leave {req.ref} created from the paper form")
            return redirect(f"/leave/{req.id}")
        else:
            C.accept_permit(db, user, cap, f.get("employee_id", ""), {k: f.get(k) for k in ("work_permit_no", "expiry_date", "passport_no")})
            db.commit()
            flash(request, "Work permit saved to the restricted record")
    except AIError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/capture/{cid}")


@router.post("/{cid}/retry", dependencies=[Depends(check_csrf)])
def retry(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    cap = _get(db, cid, user)
    C.extract(db, cap)
    db.commit()
    return redirect(f"/capture/{cid}")


@router.post("/{cid}/reject", dependencies=[Depends(check_csrf)])
def reject(request: Request, cid: int, note: str = Form(...), db: Session = Depends(get_db), user=Depends(require(*HR))):
    cap = _get(db, cid, user)
    cap.status, cap.review_note, cap.reviewed_by = "rejected", note[:500], user.label
    audit(db, user, "reject", "form_capture", cid, note)
    db.commit()
    return redirect("/capture")
