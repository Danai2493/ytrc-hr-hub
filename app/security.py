"""Sign-in, sessions, CSRF and role checks."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, h = stored.split("$")
    test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(test.hex(), h)


def password_problem(pw: str) -> Optional[str]:
    if len(pw) < 10:
        return "Password must be at least 10 characters"
    if pw.isdigit() or pw.isalpha():
        return "Use letters and numbers"
    return None


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["uid"] = user.id
    request.session["exp"] = (datetime.now() + timedelta(hours=settings.session_hours)).timestamp()
    request.session["csrf"] = secrets.token_urlsafe(24)
    if user.language:
        request.session["lang"] = user.language


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


class LoginRequired(Exception):
    pass


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    uid = request.session.get("uid")
    exp = request.session.get("exp", 0)
    if not uid or exp < datetime.now().timestamp():
        return None
    user = db.get(User, uid)
    if user is None or not user.active:
        return None
    request.state.user = user
    return user


def current_user(user: Optional[User] = Depends(current_user_optional)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not user.has(*roles):
            raise HTTPException(403, "You do not have access to this page")
        return user

    return dep


async def check_csrf(request: Request) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        form = await request.form()
        sent = form.get("_csrf") or request.headers.get("x-csrf-token")
        if not sent or not hmac.compare_digest(str(sent), request.session.get("csrf", "")):
            raise HTTPException(400, "Form expired. Please reload the page and try again.")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""
