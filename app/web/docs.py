"""Policies (library + acknowledgement) and personnel documents (HR-only scans, expiry watch)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, EmployeeDocument, OrgUnit, Policy, PolicyAck
from ..models.docs import ACK_METHODS, APPLIES_TO, DOC_TYPES
from ..security import check_csrf, current_user, require
from ..services import docs as D
from .templating import flash, redirect, render

router = APIRouter()
CTX = {"doc_types": DOC_TYPES, "applies": APPLIES_TO, "methods": ACK_METHODS}


def _units(db: Session) -> dict:
    return {u.code: u for u in db.scalars(select(OrgUnit))}


# ---------------- policy library ----------------

@router.get("/docs")
def library(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    is_hr = user.has(*D.HR)
    me = db.get(Employee, user.employee_id) if user.employee_id else None
    mine = set(db.scalars(select(PolicyAck.policy_id).where(PolicyAck.employee_id == user.employee_id))) if me else set()
    if is_hr:
        rows = list(db.scalars(select(Policy).where(Policy.status != "retired").order_by(Policy.code, Policy.version)))
    else:
        rows = [p for p in D.published(db) if me is None or D.applies(p, me) or user.has("gm", "dept_head", "auditor")]
    prog = {p.id: D.progress(db, p) for p in rows if p.status == "published"} if user.has("hr", "hr_manager", "gm", "auditor") else {}
    return render(request, "docs/library.html", {**CTX, "rows": rows, "mine": mine, "me": me, "prog": prog, "is_hr": is_hr, "tab": "policies",
                                               "todo": D.my_todo(db, user)})


def _policy(db: Session, pid: int, user) -> Policy:
    p = db.get(Policy, pid)
    if p is None or (p.status != "published" and not user.has(*D.HR, "auditor")):
        raise HTTPException(404)
    return p


@router.get("/docs/policy/new")
def policy_new(request: Request, user=Depends(require(*D.HR))):
    return render(request, "docs/policy.html", {**CTX, "p": None, "is_hr": True, "tab": "policies", "today": date.today()})


@router.post("/docs/policy/new", dependencies=[Depends(check_csrf)])
async def policy_create(request: Request, db: Session = Depends(get_db), user=Depends(require(*D.HR))):
    f = await request.form()
    up = f.get("file")
    data = await up.read() if up is not None and getattr(up, "filename", "") else b""
    try:
        p = D.save_policy(db, user, None, f, data, getattr(up, "content_type", "") or "")
        db.commit()
        flash(request, "Draft saved. The HR Manager publishes it.")
        return redirect(f"/docs/policy/{p.id}")
    except (D.DocError, ValueError) as ex:
        db.rollback()
        flash(request, str(ex), "bad")
        return redirect("/docs/policy/new")


@router.get("/docs/policy/{pid}")
def policy_page(request: Request, pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = _policy(db, pid, user)
    me = db.get(Employee, user.employee_id) if user.employee_id else None
    acked = db.scalar(select(PolicyAck).where(PolicyAck.policy_id == p.id, PolicyAck.employee_id == user.employee_id)) if me else None
    is_hr = user.has(*D.HR)
    prog = D.progress(db, p) if p.status == "published" and user.has("hr", "hr_manager", "gm", "auditor") else None
    history = list(db.scalars(select(Policy).where(Policy.code == p.code, Policy.id != p.id).order_by(Policy.version.desc())))
    return render(request, "docs/policy.html", {**CTX, "p": p, "is_hr": is_hr, "acked": acked, "can_ack": bool(me and D.applies(p, me) and p.status == "published"),
                                              "prog": prog, "history": history, "units": _units(db), "tab": "policies", "today": date.today(),
                                              "due": D.ack_due(p) if p.status == "published" else None})


@router.post("/docs/policy/{pid}", dependencies=[Depends(check_csrf)])
async def policy_act(request: Request, pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = _policy(db, pid, user)
    f = await request.form()
    do = f.get("do")
    go = f"/docs/policy/{pid}"
    try:
        if do == "save":
            up = f.get("file")
            data = await up.read() if up is not None and getattr(up, "filename", "") else b""
            D.save_policy(db, user, p, f, data, getattr(up, "content_type", "") or "")
            msg = "Saved."
        elif do == "publish":
            D.publish(db, user, p)
            msg = "Published. Everyone it applies to sees it on their home page."
        elif do == "version":
            n = D.new_version(db, user, p)
            go, msg = f"/docs/policy/{n.id}", "New version started as a draft."
        elif do == "ack":
            D.acknowledge(db, user, p)
            msg = "Thank you. Your acknowledgement is recorded."
        elif do == "briefing":
            on = date.fromisoformat(f.get("on") or date.today().isoformat())
            n = D.record_briefing(db, user, p, f.getlist("emp"), on, f.get("method") or "briefing")
            msg = f"{n} people recorded."
        else:
            raise D.DocError("Unknown action")
        db.commit()
        flash(request, msg)
    except (D.DocError, ValueError) as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(go)


@router.get("/docs/policy/{pid}/file")
def policy_file(pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = _policy(db, pid, user)
    fp = D.path_of(p.file_path)
    if fp is None:
        raise HTTPException(404)
    return FileResponse(fp, media_type=p.file_mime, filename=f"{p.code}-v{p.version}.{fp.suffix.lstrip('.')}", headers={"Cache-Control": "private, no-store"})


# ---------------- personnel documents ----------------

@router.get("/docs/watch")
def watch(request: Request, problem: str = "", db: Session = Depends(get_db), user=Depends(require(*D.HR))):
    rows = D.watchlist(db)
    counts = {k: sum(1 for r in rows if r.problem == k) for k in ("expired", "soon", "missing")}
    if problem in counts:
        rows = [r for r in rows if r.problem == problem]
    return render(request, "docs/watch.html", {**CTX, "rows": rows, "counts": counts, "problem": problem, "units": _units(db), "tab": "watch",
                                             "is_hr": True, "days": D.alert_days(db)})


@router.post("/people/{emp_id}/docs", dependencies=[Depends(check_csrf)])
async def doc_add(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*D.HR))):
    e = db.get(Employee, emp_id)
    if e is None:
        raise HTTPException(404)
    f = await request.form()
    up = f.get("file")
    data = await up.read() if up is not None and getattr(up, "filename", "") else b""
    try:
        D.add_document(db, user, e, f, data, getattr(up, "content_type", "") or "", getattr(up, "filename", "") or "")
        db.commit()
        flash(request, "Document added.")
    except (D.DocError, ValueError) as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(f"/people/{emp_id}#docs")


@router.get("/people/docs/{did}/file")
def doc_file(did: int, db: Session = Depends(get_db), user=Depends(require(*D.HR))):
    d = db.get(EmployeeDocument, did)
    fp = D.path_of(d.file_path) if d else None
    if fp is None:
        raise HTTPException(404)
    from ..services.common import audit
    audit(db, user, "doc_view", "employee", d.employee_id, f"opened {d.doc_type}")
    db.commit()
    return FileResponse(fp, media_type=d.file_mime, headers={"Cache-Control": "private, no-store"})


@router.post("/people/docs/{did}/remove", dependencies=[Depends(check_csrf)])
def doc_remove(request: Request, did: int, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    d = db.get(EmployeeDocument, did)
    if d is None:
        raise HTTPException(404)
    emp_id = d.employee_id
    try:
        D.remove_document(db, user, d)
        db.commit()
        flash(request, "Removed.")
    except D.DocError as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(f"/people/{emp_id}#docs")
