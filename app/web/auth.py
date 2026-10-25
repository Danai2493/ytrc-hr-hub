from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..i18n import LANGS
from ..models import Employee, OrgUnit, User
from ..security import check_csrf, client_ip, current_user_optional, login_session, verify_password
from ..services.common import audit
from .templating import flash, redirect, render

router = APIRouter()

_oauth = None


def oauth():
    global _oauth
    if _oauth is None and settings.google_enabled:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
        )
    return _oauth


@router.get("/login")
def login_page(request: Request, user=Depends(current_user_optional)):
    if user:
        return redirect("/")
    return render(request, "login.html", {"google": settings.google_enabled, "local": settings.local_login_enabled})


@router.post("/login", dependencies=[Depends(check_csrf)])
def login_local(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if not settings.local_login_enabled:
        return redirect("/login")
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    locked = bool(user and user.locked_until and user.locked_until > datetime.now())
    if not user or not user.active or locked or not verify_password(password, user.password_hash):
        if user and not locked:
            user.failed_logins += 1
            if user.failed_logins >= 5:
                user.locked_until = datetime.now() + timedelta(minutes=15)
                user.failed_logins = 0
        audit(db, None, "login_failed", "user", username[:60], "Local sign-in failed", ip=client_ip(request))
        db.commit()
        flash(request, "Wrong username or password" if not locked else "Too many attempts. Try again in 15 minutes.", "bad")
        return redirect("/login")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = datetime.now()
    login_session(request, user)
    audit(db, user, "login", "user", user.id, "Local sign-in", ip=client_ip(request))
    db.commit()
    return redirect("/")


@router.get("/auth/google")
async def google_start(request: Request):
    o = oauth()
    if o is None:
        return redirect("/login")
    return await o.google.authorize_redirect(request, f"{settings.base_url.rstrip('/')}/auth/google/callback")


@router.get("/auth/google/callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    o = oauth()
    if o is None:
        return redirect("/login")
    try:
        token = await o.google.authorize_access_token(request)
    except Exception:
        flash(request, "Google sign-in was cancelled or failed", "bad")
        return redirect("/login")
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    domain = email.split("@")[-1]
    if not info.get("email_verified") or (settings.google_allowed_domains and domain not in settings.google_allowed_domains):
        audit(db, None, "login_denied", "user", email, "Google account outside allowed domains", ip=client_ip(request))
        db.commit()
        flash(request, "Please sign in with your company Google account", "bad")
        return redirect("/login")
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        emp = db.scalar(select(Employee).where(Employee.work_email == email, Employee.status == "Active"))
        if emp is None:
            audit(db, None, "login_denied", "user", email, "No employee record with this email", ip=client_ip(request))
            db.commit()
            flash(request, "Your account is not in the HR system yet. Please ask HR to add your work email.", "bad")
            return redirect("/login")
        roles = ["employee"]
        if db.scalar(select(OrgUnit.code).where(OrgUnit.head_employee_id == emp.employee_id)):
            roles.append("dept_head")
        user = User(email=email, display_name=emp.full_name_en, employee_id=emp.employee_id, roles=roles)
        db.add(user)
        db.flush()
        audit(db, user, "create", "user", user.id, "Account created on first Google sign-in")
    if not user.active:
        flash(request, "Your account is switched off. Please contact HR.", "bad")
        return redirect("/login")
    user.last_login_at = datetime.now()
    login_session(request, user)
    audit(db, user, "login", "user", user.id, "Google sign-in", ip=client_ip(request))
    db.commit()
    return redirect("/")


@router.post("/logout", dependencies=[Depends(check_csrf)])
def logout(request: Request, db: Session = Depends(get_db), user=Depends(current_user_optional)):
    if user:
        audit(db, user, "logout", "user", user.id, "Signed out", ip=client_ip(request))
        db.commit()
    request.session.clear()
    return redirect("/login")


@router.post("/lang", dependencies=[Depends(check_csrf)])
def set_lang(request: Request, lang: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db), user=Depends(current_user_optional)):
    if lang in LANGS:
        request.session["lang"] = lang
        if user:
            user.language = lang
            db.commit()
    return redirect(next if next.startswith("/") and not next.startswith("//") else "/")
