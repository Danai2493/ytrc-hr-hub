"""Exit (blueprint M9): once an exit is approved, a case opens with the
clearance checklist, final pay notes, the social security notice and the exit
interview. AI reads the interview comments (Thai, Myanmar or English) and
suggests the exit reason code; HR confirms it, so the turnover analysis rests
on coded reasons instead of free text."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, EmploymentEvent, ExitCase, User
from ..models.core import EXIT_REASONS
from ..models.hrdev import EXIT_INTERVIEW_QUESTIONS
from .common import audit, get_setting, next_ref

# code, label, who ticks it (role), when it applies
CHECKLIST = [
    ("HANDOVER", "Work handed over", "dept_head", None),
    ("ASSETS", "Uniform, PPE, tools and locker key returned", "dept_head", None),
    ("ID_CARD", "Employee ID card and access card returned", "hr", None),
    ("ROOM", "Residence room returned", "hr", "room"),
    ("IT_ACCESS", "Email and system accounts closed", "admin", "it"),
    ("LOANS", "Loans and advances settled", "hr", None),
    ("FINAL_PAY", "Final pay input sent to Bplus (last OT, unused vacation)", "hr", None),
    ("SSO", "Social security told of the exit (SPS 6-09)", "hr", None),
    ("PERMIT", "Department of Employment told (foreign worker, within 15 days)", "hr", "foreign"),
    ("CERT", "Employment certificate given", "hr", None),
]

REASON_CODES = [c for c, _ in EXIT_REASONS if c.startswith("RES-")]


def sso_due(db: Session, last_day: date) -> date:
    """The social security office must be told by the 15th of the next month."""
    day = int(get_setting(db, "sso_notice_day") or 15)
    first_next = (last_day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first_next.replace(day=day)


def open_case(db: Session, emp: Employee, last_day: date, reason_code: Optional[str], user: Optional[User], approval_id: Optional[int] = None) -> ExitCase:
    case = db.scalar(select(ExitCase).where(ExitCase.employee_id == emp.employee_id, ExitCase.status == "open"))
    if case is not None:
        case.last_day, case.reason_code = last_day, reason_code or case.reason_code
        return case
    from ..models import User as U
    from .leave import balances

    has_login = bool(emp.work_email) or db.scalar(select(U.id).where(U.employee_id == emp.employee_id)) is not None
    items = []
    for code, label, owner, when in CHECKLIST:
        if when == "room" and not (emp.restricted and emp.restricted.residence_room):
            continue
        if when == "it" and not has_login:
            continue
        if when == "foreign" and not emp.is_foreign_worker:
            continue
        items.append({"code": code, "label": label, "owner": owner, "done": False})
    vac = next((b for b in balances(db, emp, last_day.year) if b.type.code == "VAC"), None)
    case = ExitCase(
        ref=next_ref(db, "EX", last_day.year), employee_id=emp.employee_id, last_day=last_day,
        reason_code=reason_code, items=items, approval_id=approval_id,
        vacation_days_left=max(vac.left, 0) if vac and vac.left is not None else None,
    )
    case.employee = emp
    db.add(case)
    db.flush()
    audit(db, user, "create", "exit_case", case.ref, f"Exit case for {emp.employee_id}, last day {last_day}")
    return case


def can_tick(user: User, item: dict) -> bool:
    owner = item.get("owner")
    if user.has("hr", "hr_manager"):
        return True
    if owner == "admin":
        return user.has("admin")
    if owner == "dept_head":
        return user.has("dept_head", "supervisor")
    return False


def tick(db: Session, case: ExitCase, user: User, code: str, done: bool, note: str = "") -> None:
    items = [dict(i) for i in case.items]
    for i in items:
        if i["code"] == code:
            if not can_tick(user, i):
                raise PermissionError("Not your item")
            i["done"] = done
            i["by"] = user.label if done else None
            i["at"] = datetime.now().isoformat(timespec="minutes") if done else None
            if note:
                i["note"] = note[:200]
            audit(db, user, "tick" if done else "untick", "exit_case", case.ref, f"{code}: {i['label']}")
    case.items = items
    if code == "IT_ACCESS" and done:
        _deactivate_logins(db, case, user)


def _deactivate_logins(db: Session, case: ExitCase, user: User) -> None:
    from ..models import User as U

    for u in db.scalars(select(U).where(U.employee_id == case.employee_id, U.active.is_(True))):
        u.active = False
        audit(db, user, "deactivate", "user", u.id, f"Login closed at exit ({case.ref})")


def close(db: Session, case: ExitCase, user: User) -> None:
    if case.open_items:
        raise ValueError("Tick every checklist item first")
    case.status = "closed"
    case.closed_at = datetime.now()
    audit(db, user, "close", "exit_case", case.ref, "Clearance complete")


# ---------- exit interview ----------

INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reason_code": {"type": "string", "enum": REASON_CODES},
        "confidence": {"type": "number"},
        "summary_en": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reason_code", "confidence", "summary_en", "themes"],
    "additionalProperties": False,
}

INTERVIEW_PROMPT = """You help HR at a Thai rubber factory code exit interviews. The answers below
come from one employee who resigned. Comments may be in Thai, Burmese or English.
Scores run from 1 (very unhappy) to 5 (very happy).

Choose the ONE main reason code that best explains why the person left:
{codes}
Then give your confidence from 0 to 1, a neutral two-sentence summary in English (no names,
no ID numbers), and up to four short themes (e.g. "night shift", "supervisor tone").
If the answers do not show a clear reason, use RES-OTHER with a low confidence.

Interview:
{interview}"""


def save_interview(db: Session, case: ExitCase, user: User, scores: dict, would_return: str, main_reason: str, comments: str) -> None:
    clean = {}
    for q, _ in EXIT_INTERVIEW_QUESTIONS:
        try:
            v = int(scores.get(q) or 0)
            if 1 <= v <= 5:
                clean[q] = v
        except (TypeError, ValueError):
            continue
    case.interview = {
        "scores": clean, "would_return": would_return or None, "main_reason": (main_reason or "")[:1000],
        "comments": (comments or "")[:3000], "by": user.label, "at": datetime.now().isoformat(timespec="minutes"),
    }
    audit(db, user, "save", "exit_interview", case.ref, "Exit interview recorded")


def suggest_reason(db: Session, case: ExitCase) -> Optional[str]:
    """Ask the AI for a reason code. Returns an error message, or None when done."""
    from .ai.providers import AIError, get_provider

    iv = case.interview or {}
    if not iv:
        return "Record the interview first"
    names = dict(EXIT_REASONS)
    codes = "\n".join(f"- {c}: {names[c]}" for c in REASON_CODES)
    lines = [f"{label}: {iv.get('scores', {}).get(q, '-')}" for q, label in EXIT_INTERVIEW_QUESTIONS]
    lines.append(f"Would come back: {iv.get('would_return') or '-'}")
    lines.append(f"Main reason in their words: {iv.get('main_reason') or '-'}")
    lines.append(f"Other comments: {iv.get('comments') or '-'}")
    prompt = INTERVIEW_PROMPT.format(codes=codes, interview="\n".join(lines))
    provider = get_provider()
    try:
        res = provider.classify(prompt, INTERVIEW_SCHEMA, {"task": "exit_interview", "interview": iv})
    except AIError as e:
        return str(e)
    except Exception as e:  # network, bad JSON
        return f"{type(e).__name__}: {e}"[:300]
    data = res.data
    if data.get("reason_code") not in REASON_CODES:
        return "The AI answer had no valid reason code"
    case.ai_reason_code = data["reason_code"]
    case.ai_confidence = float(data.get("confidence") or 0)
    case.ai_summary = (data.get("summary_en") or "")[:2000]
    case.ai_themes = [str(t)[:60] for t in (data.get("themes") or [])][:4]
    audit(db, None, "ai_suggest", "exit_case", case.ref, f"{res.provider}: {case.ai_reason_code} ({case.ai_confidence:.2f})")
    return None


def confirm_reason(db: Session, case: ExitCase, user: User, code: str) -> None:
    if code not in dict(EXIT_REASONS):
        raise ValueError("Unknown reason code")
    emp = case.employee
    before = emp.exit_reason_code
    case.reason_code = code
    case.reason_confirmed_by = user.label
    emp.exit_reason_code = code
    ev = db.scalar(select(EmploymentEvent).where(EmploymentEvent.employee_id == emp.employee_id, EmploymentEvent.event_type == "exit").order_by(EmploymentEvent.id.desc()))
    if ev is not None:
        ev.reason_code = code
    audit(db, user, "update", "employee", emp.employee_id, f"Exit reason confirmed: {code}", {"before": {"exit_reason_code": before}, "after": {"exit_reason_code": code}})


def interview_averages(db: Session, since: date, emp_ids: Optional[set[str]] = None) -> tuple[dict[str, float], int]:
    sums: dict[str, list[int]] = {q: [] for q, _ in EXIT_INTERVIEW_QUESTIONS}
    n = 0
    for c in db.scalars(select(ExitCase).where(ExitCase.last_day >= since)):
        if emp_ids is not None and c.employee_id not in emp_ids:
            continue
        sc = (c.interview or {}).get("scores") or {}
        if sc:
            n += 1
        for q in sums:
            if q in sc:
                sums[q].append(sc[q])
    return {q: round(sum(v) / len(v), 1) for q, v in sums.items() if v}, n

