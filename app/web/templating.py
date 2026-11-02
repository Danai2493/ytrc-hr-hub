from __future__ import annotations

from contextvars import ContextVar
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
_lang: ContextVar[str] = ContextVar("lang", default=settings.default_language)
# Shared macros (partials/charts.html) are imported without the page context; they translate through this.
templates.env.globals["_"] = lambda text, **kw: gettext(_lang.get(), text, **kw)

# (path, label, roles allowed; None = everyone signed in)
NAV = [
    # (path, label, roles allowed or None = everyone signed in, icon, menu group)
    ("/", "Home", None, "home", ""),
    ("/approvals", "Approvals", None, "check", ""),
    ("/people", "People", ("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor"), "users", "People"),
    ("/org", "Org & positions", ("hr", "hr_manager", "gm", "dept_head", "auditor"), "org", "People"),
    ("/recruit", "Recruitment", ("hr", "hr_manager", "gm", "dept_head", "supervisor"), "user-plus", "People"),
    ("/newstaff", "New employees", ("hr", "hr_manager", "dept_head", "supervisor", "admin"), "clipboard", "People"),
    ("/exit", "Exit", ("hr", "hr_manager", "dept_head", "admin"), "log-out", "People"),
    ("/er", "Employee relations", ("hr", "hr_manager", "gm", "dept_head", "supervisor"), "alert", "People"),
    ("/ot", "OT", None, "clock", "Time and pay"),
    ("/leave", "Leave", None, "calendar", "Time and pay"),
    ("/attendance", "Attendance", ("hr", "hr_manager", "gm", "dept_head", "supervisor"), "scan", "Time and pay"),
    ("/capture", "Paper forms (AI)", ("hr", "hr_manager", "supervisor"), "camera", "Time and pay"),
    ("/pay", "Pay input", ("hr", "hr_manager"), "wallet", "Time and pay"),
    ("/skills", "Skills & training", ("hr", "hr_manager", "gm", "dept_head", "supervisor"), "cap", "Develop"),
    ("/perform", "Performance", None, "target", "Develop"),
    ("/growth", "Growth", None, "sprout", "Develop"),
    ("/talent", "Talent review", ("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor"), "grid", "Develop"),
    ("/dash", "Dashboards", ("hr", "hr_manager", "gm", "dept_head", "auditor"), "chart", "Insight"),
    ("/plan", "Plan ahead", ("hr", "hr_manager", "gm", "dept_head", "auditor"), "compass", "Insight"),
    ("/pulse", "Pulse survey", None, "heart", "Insight"),
    ("/health", "Data health", ("hr", "hr_manager", "gm", "dept_head"), "shield", "Insight"),
    ("/admin", "Admin", ("admin", "hr_manager"), "settings", "System"),
]
# Phone bottom bar: the four places people go most, plus the full menu
MOBILE_NAV = ("/", "/approvals", "/ot", "/leave")


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
    _lang.set(lang)
    user = getattr(request.state, "user", None)

    def _(text: str, **kw) -> str:
        return gettext(lang, text, **kw)

    nav = [(p, _(label), icon, group) for p, label, roles, icon, group in NAV if user and (roles is None or user.has(*roles))]
    path = request.url.path
    active = max((p for p, *_x in nav if path == p or (p != "/" and path.startswith(p))), key=len, default="")
    groups: list[tuple[str, list]] = []
    for item in nav:
        if not groups or groups[-1][0] != item[3]:
            groups.append((item[3], []))
        groups[-1][1].append(item)
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
        "nav_groups": [(_(g) if g else "", items) for g, items in groups],
        "nav_active": active,
        "mobile_nav": [n for n in nav if n[0] in MOBILE_NAV],
        "inbox_count": _inbox_count(user),
        "path": path,
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


def _inbox_count(user) -> int:
    """Approvals waiting for this person, for the badge in the menu."""
    if not user:
        return 0
    from ..db import SessionLocal
    from ..services.approvals import inbox

    db = SessionLocal()
    try:
        return len(inbox(db, user))
    except Exception:  # the badge must never break a page
        return 0
    finally:
        db.close()


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    items = request.session.get("flash", [])
    items.append({"msg": msg, "kind": kind})
    request.session["flash"] = items


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)
