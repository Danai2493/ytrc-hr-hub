"""Employee relations cases. Rules of use are in app/models/relations.py."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, ErCase, User
from ..models.relations import CATEGORIES, DECIDER, KINDS, OUTCOMES, SERIOUS
from .common import audit, get_setting, next_ref, user_scope

HR = ("hr", "hr_manager")
REPLY_DAYS = 15  # default days to answer a complaint; Admin setting er_reply_days


class ErError(ValueError):
    pass


def finding(level: str, code: str, **params) -> dict:
    return {"level": level, "code": code, "params": params}


def _log(c: ErCase, user: User, text: str) -> None:
    c.log = list(c.log or []) + [{"at": datetime.now().isoformat(timespec="minutes"), "by": user.label, "text": text}]


def involved(user: User, c: ErCase) -> bool:
    return bool(user.employee_id) and user.employee_id in (c.employee_id, c.other_employee_id)


def can_see(db: Session, user: User, c: ErCase) -> bool:
    if involved(user, c):
        return False  # people in the case never see the file here
    if c.kind == "harassment" or c.confidential:
        return user.has("hr_manager", "gm")
    if c.kind == "grievance":
        return user.has("hr", "hr_manager", "gm")
    return user.has("hr", "hr_manager", "gm") or (user.has("dept_head", "supervisor") and user_scope(db, user).allows_unit(c.org_unit_code))


def can_work(user: User, c: ErCase) -> bool:
    """Record statements, findings and propose an outcome."""
    if involved(user, c) or c.status == "closed":
        return False
    return user.has("hr_manager") if c.kind == "harassment" else user.has(*HR)


def visible(db: Session, user: User, status: str = "", kind: str = "") -> list[ErCase]:
    q = select(ErCase).order_by(ErCase.reported_on.desc(), ErCase.id.desc())
    if status:
        q = q.where(ErCase.status == status)
    if kind:
        q = q.where(ErCase.kind == kind)
    return [c for c in db.scalars(q) if can_see(db, user, c)]


def warnings_in_force(db: Session, emp_id: str, on: date, category: Optional[str] = None, skip: Optional[int] = None) -> list[ErCase]:
    q = select(ErCase).where(ErCase.employee_id == emp_id, ErCase.outcome == "written", ErCase.decided_at.is_not(None), ErCase.warning_until >= on)
    if category:
        q = q.where(ErCase.category == category)
    return [c for c in db.scalars(q.order_by(ErCase.incident_on)) if c.id != skip]


def open_case(db: Session, user: User, data: dict) -> ErCase:
    kind = data.get("kind")
    if kind not in KINDS:
        raise ErError("Choose the kind of case")
    if kind != "discipline" and not user.has(*HR):
        raise ErError("Complaints are recorded by HR")
    emp = db.get(Employee, (data.get("employee_id") or "").strip().upper())
    if emp is None:
        raise ErError("Employee not found")
    if involved(user, ErCase(employee_id=emp.employee_id, other_employee_id=(data.get("other_employee_id") or "").strip().upper() or None)):
        raise ErError("You cannot open a case about yourself")
    if not user.has(*HR) and not user_scope(db, user).allows_unit(emp.org_unit_code):
        raise ErError("This employee is outside your units")
    try:
        inc = date.fromisoformat(data.get("incident_on") or "")
        rep = date.fromisoformat(data.get("reported_on") or "") if data.get("reported_on") else date.today()
    except ValueError:
        raise ErError("Check the dates")
    if inc > date.today() or rep < inc:
        raise ErError("Check the dates")
    summary = (data.get("summary") or "").strip()
    if len(summary) < 20:
        raise ErError("Describe what happened: who, what, when, where (at least 20 characters)")
    other = (data.get("other_employee_id") or "").strip().upper() or None
    if other and db.get(Employee, other) is None:
        raise ErError("Employee not found")
    cat = "harassment" if kind == "harassment" else data.get("category")
    if cat not in CATEGORIES:
        raise ErError("Choose a category")
    c = ErCase(ref=next_ref(db, "ER", rep.year), kind=kind, category=cat, employee_id=emp.employee_id, other_employee_id=other,
               org_unit_code=emp.org_unit_code, incident_on=inc, reported_on=rep, reported_by=user.label, summary=summary,
               confidential=kind == "harassment" or bool(data.get("confidential")), status="open", log=[])
    if kind != "discipline":
        c.reply_due = rep + timedelta(days=int(get_setting(db, "er_reply_days") or REPLY_DAYS))
    _log(c, user, "Case opened")
    db.add(c)
    db.flush()
    audit(db, user, "er_open", "er_case", c.id, f"{c.ref} {kind} {emp.employee_id}")
    return c


def save_work(db: Session, user: User, c: ErCase, statement: str, statement_on: str, findings: str) -> None:
    if not can_work(user, c):
        raise ErError("You cannot change this case")
    if statement.strip() and statement.strip() != (c.statement or ""):
        c.statement = statement.strip()
        c.statement_on = date.fromisoformat(statement_on) if statement_on else date.today()
        _log(c, user, "Employee statement recorded")
    if findings.strip() and findings.strip() != (c.findings or ""):
        c.findings = findings.strip()
        _log(c, user, "Findings updated")
    if c.status == "open" and (c.statement or c.findings):
        c.status = "investigating"
    audit(db, user, "er_work", "er_case", c.id, c.ref)


def checks(db: Session, c: ErCase, outcome: str, reasons: str, susp_from: Optional[date], susp_days: Optional[int], pay_ok: bool,
           today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    out = []
    active = warnings_in_force(db, c.employee_id, today, skip=c.id)
    if active and c.kind == "discipline":
        out.append(finding("info", "er_active_warnings", n=len(active), refs=", ".join(w.ref for w in active)))
    if c.kind != "discipline":
        return out
    action = outcome in ("verbal", "written", "suspension", "dismissal")
    if action and not c.statement:
        out.append(finding("block", "er_no_statement"))
    if outcome in ("written", "suspension", "dismissal") and len(reasons.strip()) < 20:
        out.append(finding("block", "er_no_reasons"))
    if action and (today - c.incident_on).days > 60:
        out.append(finding("flag", "er_old_offence", days=(today - c.incident_on).days))
    if outcome == "suspension":
        if not susp_from:
            out.append(finding("block", "er_susp_from"))
        if not susp_days or susp_days > 7 or susp_days < 1:
            out.append(finding("block", "er_susp_days"))
        if not pay_ok:
            out.append(finding("block", "er_susp_pay"))
    if outcome == "dismissal":
        out.append(finding("flag", "er_dismiss_reasons"))
        same = warnings_in_force(db, c.employee_id, c.incident_on, c.category, skip=c.id)
        if same:
            out.append(finding("flag", "er_repeat", ref=same[0].ref, until=same[0].warning_until.strftime("%d/%m/%Y")))
        elif c.category in SERIOUS:
            out.append(finding("flag", "er_serious", cat=CATEGORIES[c.category]))
        else:
            out.append(finding("flag", "er_no_warning"))
    return out


def propose(db: Session, user: User, c: ErCase, form) -> list[dict]:
    if not can_work(user, c):
        raise ErError("You cannot change this case")
    outcome = form.get("outcome")
    if outcome not in OUTCOMES[c.kind]:
        raise ErError("Choose an outcome")
    reasons = (form.get("reasons") or "").strip()
    try:
        sf = date.fromisoformat(form.get("suspension_from")) if form.get("suspension_from") else None
        sd = int(form.get("suspension_days")) if form.get("suspension_days") else None
    except ValueError:
        raise ErError("Check the dates")
    pay_ok = bool(form.get("suspension_pay_ok"))
    found = checks(db, c, outcome, reasons, sf, sd, pay_ok)
    if any(f["level"] == "block" for f in found):
        return found
    c.outcome, c.reasons = outcome, reasons or None
    c.suspension_from, c.suspension_days, c.suspension_pay_ok = (sf, sd, pay_ok) if outcome == "suspension" else (None, None, False)
    c.status, c.proposed_by, c.proposed_at = "decision", user.label, datetime.now()
    c.decided_by = c.decided_at = None
    _log(c, user, f"Outcome proposed: {OUTCOMES[c.kind][outcome]}")
    audit(db, user, "er_propose", "er_case", c.id, f"{c.ref} {outcome}")
    return found


def decider_roles(c: ErCase) -> tuple[str, ...]:
    if c.kind == "harassment":
        return ("gm",) if c.outcome == "upheld" else ("hr_manager", "gm")
    return DECIDER.get(c.outcome or "", ("hr_manager",))


def can_decide(user: User, c: ErCase) -> bool:
    return (c.status == "decision" and user.has(*decider_roles(c)) and not involved(user, c)
            and user.label != c.proposed_by)


def decide(db: Session, user: User, c: ErCase, agree: bool, note: str) -> None:
    if not can_decide(user, c):
        raise ErError("Someone other than the proposer decides, at the right level")
    if agree:
        c.decided_by, c.decided_at = user.label, datetime.now()
        if c.outcome == "written":
            c.warning_until = c.incident_on + timedelta(days=365)
        _log(c, user, "Decision agreed" + (f": {note}" if note else ""))
    else:
        if not note:
            raise ErError("Say what needs to change")
        c.status, c.outcome = "investigating", None
        _log(c, user, f"Sent back: {note}")
    audit(db, user, "er_decide", "er_case", c.id, f"{c.ref} {'agreed' if agree else 'sent back'}")


def close(db: Session, user: User, c: ErCase, letter_on: str, signed: str) -> None:
    if not can_work(user, c) or not c.decided_at:
        raise ErError("Close the case after the decision")
    if c.outcome in ("written", "suspension", "dismissal", "verbal", "upheld", "partly", "not_upheld"):
        if not letter_on:
            raise ErError("Give the date the employee was told or given the letter")
        c.letter_given_on = date.fromisoformat(letter_on)
        c.employee_signed = {"yes": True, "no": False}.get(signed)
    c.status, c.closed_at = "closed", datetime.now()
    _log(c, user, "Case closed")
    audit(db, user, "er_close", "er_case", c.id, c.ref)
