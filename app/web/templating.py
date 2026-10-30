from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..i18n import LANGS, gettext, rule_text
from ..models.time import DAY_CODES, PAY_CODES
from ..security import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# (path, label, roles allowed; None = everyone signed in)
NAV = [
    ("/", "Home", None),
    ("/people", "People", ("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor")),
    ("/org", "Org & positions", ("hr", "hr_manager", "gm", "dept_head", "auditor")),
    ("/ot", "OT", None),
    ("/leave", "Leave", None),
    ("/attendance", "Attendance", ("hr", "hr_manager", "gm", "dept_head", "supervisor")),
    ("/approvals", "Approvals", None),
    ("/exit", "Exit", ("hr", "hr_manager", "dept_head", "admin")),
    ("/capture", "Paper forms (AI)", ("hr", "hr_manager", "supervisor")),
    ("/pay", "Pay input", ("hr", "hr_manager")),
    ("/health", "Data health", ("hr", "hr_manager", "gm", "dept_head")),
    ("/dash", "Dashboards", ("hr", "hr_manager", "gm", "dept_head", "auditor")),
    ("/admin", "Admin", ("admin", "hr_manager")),
]


def lang_of(request: Request) -> str:
    lang = request.session.get("lang") or settings.default_language
    return lang if lang in LANGS else "en"


def fmt_date(d: Any) -> str:
    if not d:
        return ""
    if isinstance(d, str):
        return d
    return d.strftime("%d/%m/%Y")


def fmt_dt(d: Optional[datetime]) -> str:
    return d.strftime("%d/%m/%Y %H:%M") if d else ""


def fmt_time(d: Optional[datetime]) -> str:
    return d.strftime("%H:%M") if d else ""


def hours(minutes: Optional[int]) -> str:
    if minutes is None:
        return "-"
    h = minutes / 60
    return f"{h:.2f}".rstrip("0").rstrip(".")


def render(request: Request, name: str, ctx: Optional[dict] = None, status: int = 200) -> HTMLResponse:
    lang = lang_of(request)
    user = getattr(request.state, "user", None)

    def _(text: str, **kw) -> str:
        return gettext(lang, text, **kw)

    nav = [(p, _(label)) for p, label, roles in NAV if user and (roles is None or user.has(*roles))]
    flashes = request.session.pop("flash", []) if "flash" in request.session else []
    base = {
        "request": request,
        "user": user,
        "lang": lang,
        "langs": LANGS,
        "_": _,
        "rt": lambda f: rule_text(lang, f),
        "csrf": csrf_token(request),
        "settings": settings,
        "nav": nav,
        "path": request.url.path,
        "flashes": flashes,
        "fmt_date": fmt_date,
        "fmt_dt": fmt_dt,
        "fmt_time": fmt_time,
        "hours": hours,
        "today": date.today(),
        "PAY_CODES": PAY_CODES,
        "DAY_CODES": DAY_CODES,
    }
    base.update(ctx or {})
    return templates.TemplateResponse(request, name, base, status_code=status)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    items = request.session.get("flash", [])
    items.append({"msg": msg, "kind": kind})
    request.session["flash"] = items


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
