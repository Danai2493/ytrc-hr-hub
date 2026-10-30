from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import engine
from .security import LoginRequired
from .web import admin, approvals, attendance, auth, capture, dash, exits, health, home, leave, org, ot, pay, people
from .web.templating import redirect, render

# Register approval hooks
from .services import ot as _ot  # noqa: F401
from .services import people as _people  # noqa: F401

log = logging.getLogger("hrhub")

if settings.is_production and settings.secret_key in ("", "dev-only-change-me"):
    raise RuntimeError("Set SECRET_KEY to a long random value in production")

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="hrhub_session",
    max_age=settings.session_hours * 3600,
    same_site="lax",
    https_only=settings.base_url.startswith("https"),
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'",
    )
    if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for r in (auth, home, people, org, ot, leave, attendance, approvals, capture, exits, pay, health, dash, admin):
    app.include_router(r.router)


@app.exception_handler(LoginRequired)
async def _login(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api"):
        return JSONResponse({"error": "login required"}, 401)
    return redirect("/login")


@app.exception_handler(HTTPException)
async def _http(request: Request, exc: HTTPException):
    return render(request, "error.html", {"code": exc.status_code, "detail": exc.detail}, exc.status_code)


@app.get("/healthz", include_in_schema=False)
def healthz():
    with engine.connect() as c:
        c.execute(text("select 1"))
    return PlainTextResponse("ok")
