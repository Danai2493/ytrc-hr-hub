from __future__ import annotations

import json
import secrets
from datetime import date, time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Calendar, Employee, Holiday, OrgUnit, OTBudget, Shift, User, UserScope
from ..models.system import ROLES
from ..security import check_csrf, hash_password, password_problem, require
from ..services.common import DEFAULT_SETTINGS, audit, get_setting, set_setting
from .templating import flash, redirect, render

router = APIRouter(prefix="/admin")
ADMIN = ("admin", "hr_manager")


@router.get("")
def admin_home(request: Request, db: Session = Depends(get_db), user=Depends(require(*ADMIN))):
    users = db.scalars(select(User).order_by(User.display_name)).all()
    return render(request, "admin/home.html", {
        "users": users, "roles": ROLES,
        "settings_rows": [(k, get_setting(db, k), d) for k, (_v, d) in DEFAULT_SETTINGS.items()],
        "shifts": db.scalars(select(Shift).order_by(Shift.code)).all(),
        "calendars": db.scalars(select(Calendar)).all(),
        "holidays": db.scalars(select(Holiday).where(Holiday.day >= date(date.today().year, 1, 1)).order_by(Holiday.day)).all(),
        "budgets": db.scalars(select(OTBudget).order_by(OTBudget.month.desc()).limit(24)).all(),
        "units": db.scalars(select(OrgUnit).order_by(OrgUnit.code)).all(),
    })


@router.get("/user/{uid}")
def user_form(request: Request, uid: str, db: Session = Depends(get_db), user=Depends(require(*ADMIN))):
    u = db.get(User, int(uid)) if uid != "new" else None
    return render(request, "admin/user.html", {"u": u, "roles": ROLES, "units": db.scalars(select(OrgUnit).order_by(OrgUnit.code)).all()})


@router.post("/user/{uid}", dependencies=[Depends(check_csrf)])
async def user_save(request: Request, uid: str, db: Session = Depends(get_db), user=Depends(require(*ADMIN))):
    f = await request.form()
    roles = [r for r in f.getlist("roles") if r in ROLES]
    # Only an admin can hand out admin; HR Manager manages HR roles.
    if "admin" in roles and not user.has("admin"):
        roles.remove("admin")
    if uid == "new":
        u = User(display_name=f.get("display_name") or "User", roles=roles)
        db.add(u)
    else:
        u = db.get(User, int(uid))
        if u is None:
            raise HTTPException(404)
        if u.has("admin") and not user.has("admin"):
            flash(request, "Only an admin can change an admin account", "bad")
            return redirect("/admin")
        u.roles = roles
        u.display_name = f.get("display_name") or u.display_name
    u.email = (f.get("email") or "").strip().lower() or None
    u.username = (f.get("username") or "").strip().lower() or None
    emp_id = (f.get("employee_id") or "").strip().upper() or None
    if emp_id and not db.get(Employee, emp_id):
        flash(request, f"Employee {emp_id} not found", "bad")
        db.rollback()
        return redirect(f"/admin/user/{uid}")
    u.employee_id = emp_id
    u.active = f.get("active") == "on"
    pw = f.get("password") or ""
    if pw:
        problem = password_problem(pw)
        if problem:
            flash(request, problem, "bad")
            db.rollback()
            return redirect(f"/admin/user/{uid}")
        u.password_hash = hash_password(pw)
    db.flush()
    for s in list(u.scopes):
        db.delete(s)
    db.flush()
    for code in f.getlist("scopes"):
        if db.get(OrgUnit, code):
            u.scopes.append(UserScope(org_unit_code=code))
    audit(db, user, "save", "user", u.id, f"{u.label}: roles {', '.join(roles)}" + (" (password set)" if pw else ""))
    db.commit()
    flash(request, "User saved")
    return redirect("/admin")


@router.post("/user/{uid}/temp-password", dependencies=[Depends(check_csrf)])
def temp_password(request: Request, uid: int, db: Session = Depends(get_db), user=Depends(require(*ADMIN))):
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404)
    pw = secrets.token_urlsafe(8) + "7a"
    u.password_hash = hash_password(pw)
    u.failed_logins, u.locked_until = 0, None
    audit(db, user, "reset_password", "user", uid, u.label)
    db.commit()
    # Shown once on this page, never stored or put in the session cookie.
    return render(request, "admin/user.html", {"u": u, "roles": ROLES, "units": db.scalars(select(OrgUnit).order_by(OrgUnit.code)).all(), "temp_pw": pw})


@router.post("/settings", dependencies=[Depends(check_csrf)])
async def settings_save(request: Request, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    f = await request.form()
    changed = []
    for key, (default, _d) in DEFAULT_SETTINGS.items():
        if key not in f:
            continue
        raw = (f.get(key) or "").strip()
        if isinstance(default, (list, dict)):
            try:
                val = json.loads(raw) if raw else default
            except json.JSONDecodeError:
                flash(request, f"{key}: not valid JSON", "bad")
                continue
        elif raw == "":
            val = None
        else:
            try:
                val = float(raw) if "." in raw else int(raw)
            except ValueError:
                val = raw
        if val != get_setting(db, key):
            set_setting(db, key, val)
            changed.append(key)
    audit(db, user, "update", "setting", None, ", ".join(changed))
    db.commit()
    flash(request, f"Settings saved: {', '.join(changed) or 'no change'}")
    return redirect("/admin#settings")


@router.post("/shift", dependencies=[Depends(check_csrf)])
def shift_save(request: Request, code: str = Form(...), name: str = Form(...), start: str = Form(...), end: str = Form(...), break_minutes: int = Form(60), db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    code = code.strip().upper()
    s = db.get(Shift, code) or Shift(code=code)
    s.name, s.start, s.end, s.break_minutes = name, time.fromisoformat(start), time.fromisoformat(end), break_minutes
    db.add(s)
    audit(db, user, "save", "shift", code, f"{start}-{end}")
    db.commit()
    return redirect("/admin#shifts")


@router.post("/holiday", dependencies=[Depends(check_csrf)])
def holiday_add(request: Request, calendar_code: str = Form(...), day: str = Form(...), name: str = Form(...), db: Session = Depends(get_db), user=Depends(require("hr_manager", "hr"))):
    d = date.fromisoformat(day)
    if db.scalar(select(Holiday).where(Holiday.calendar_code == calendar_code, Holiday.day == d)):
        flash(request, "That date is already a holiday on this calendar", "bad")
    else:
        db.add(Holiday(calendar_code=calendar_code, day=d, name=name))
        audit(db, user, "create", "holiday", f"{calendar_code}:{d}", name)
        db.commit()
    return redirect("/admin#holidays")


@router.post("/holiday/{hid}/delete", dependencies=[Depends(check_csrf)])
def holiday_delete(request: Request, hid: int, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    h = db.get(Holiday, hid)
    if h:
        audit(db, user, "delete", "holiday", f"{h.calendar_code}:{h.day}", h.name)
        db.delete(h)
        db.commit()
    return redirect("/admin#holidays")


@router.post("/budget", dependencies=[Depends(check_csrf)])
def budget_save(request: Request, month: str = Form(...), org_unit_code: str = Form(""), employee_group: str = Form(""), hours: str = Form(""), amount_thb: str = Form(""), db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    m = date.fromisoformat(month + "-01")
    unit = org_unit_code or None
    group = employee_group or None
    b = db.scalar(select(OTBudget).where(OTBudget.month == m, OTBudget.org_unit_code == unit, OTBudget.employee_group == group)) if unit or group else db.scalar(select(OTBudget).where(OTBudget.month == m, OTBudget.org_unit_code.is_(None), OTBudget.employee_group.is_(None)))
    if b is None:
        b = OTBudget(month=m, org_unit_code=unit, employee_group=group)
        db.add(b)
    b.hours = float(hours) if hours else None
    b.amount_thb = float(amount_thb) if amount_thb else None
    audit(db, user, "save", "ot_budget", f"{month}:{unit}:{group}", f"{hours}h {amount_thb} THB")
    db.commit()
    return redirect("/admin#budget")


@router.get("/audit")
def audit_log(request: Request, q: str = "", action: str = "", db: Session = Depends(get_db), user=Depends(require("admin", "hr_manager", "auditor"))):
    stmt = select(AuditLog).order_by(AuditLog.at.desc()).limit(500)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if q:
        like = f"%{q}%"
        stmt = stmt.where((AuditLog.entity_id.ilike(like)) | (AuditLog.user_label.ilike(like)) | (AuditLog.summary.ilike(like)))
    rows = db.scalars(stmt).all()
    return render(request, "admin/audit.html", {"rows": rows, "q": q, "action": action})
