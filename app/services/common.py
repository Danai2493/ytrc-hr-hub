"""Small shared helpers: running numbers, settings, audit log, scope."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Employee, OrgUnit, Sequence, Setting, User

ID_RE = {
    "YTR": re.compile(r"^YTR\d{3}$"),
    "YTOP": re.compile(r"^YTOP\d{3}$"),
}

DEFAULT_SETTINGS: dict[str, tuple[Any, str]] = {
    "weekly_ot_limit_hours": (36, "Legal weekly limit for OT plus holiday work (hours)"),
    "daily_ot_limit_hours": (None, "Optional daily OT limit; empty = no daily check"),
    "scan_window_before_min": (120, "Scan window opens this many minutes before shift start"),
    "scan_window_after_min": (600, "Scan window closes this many minutes after shift end"),
    "scan_tolerance_min": (10, "Minutes of OT the scans may miss before it is flagged"),
    "unrequested_ot_min": (30, "Scan OT above this with no request is flagged"),
    "pay_variance_pct": (30, "Flag a person or unit whose OT hours change more than this % vs last period"),
    "work_permit_alert_days": (60, "Flag work permits expiring within this many days"),
    "probation_alert_days": (14, "Flag probation evaluations not done this many days before the end"),
    "staff_ot_cutoff_day": (20, "Staff monthly OT period ends on this day (starts the day after, previous month)"),
    "juvenile_age": (18, "Workers under this age: no OT, no holiday work, no night work"),
    "bplus_export_columns": (
        ["employee_id", "pay_code", "hours", "period_start", "period_end"],
        "Column order of the Bplus import file (adjust when the Bplus layout is confirmed)",
    ),
    "bplus_pay_code_map": (
        {"OT15": "OT15", "HOL10": "HOL10", "HOL20": "HOL20", "OT30": "OT30"},
        "App pay code -> Bplus pay code",
    ),
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULT_SETTINGS.get(key, (None, ""))[0]


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value, description=DEFAULT_SETTINGS.get(key, (None, ""))[1])
        db.add(row)
    else:
        row.value = value


def next_number(db: Session, name: str) -> int:
    seq = db.scalar(select(Sequence).where(Sequence.name == name).with_for_update())
    if seq is None:
        seq = Sequence(name=name, value=0)
        db.add(seq)
        db.flush()
    seq.value += 1
    db.flush()
    return seq.value


def next_ref(db: Session, prefix: str, year: int) -> str:
    n = next_number(db, f"{prefix}-{year}")
    return f"{prefix}-{year}-{n:06d}"


def valid_employee_id(emp_id: str, group: Optional[str] = None) -> bool:
    if not emp_id:
        return False
    if group == "Operator":
        return bool(ID_RE["YTOP"].match(emp_id))
    if group:
        return bool(ID_RE["YTR"].match(emp_id))
    return bool(ID_RE["YTR"].match(emp_id) or ID_RE["YTOP"].match(emp_id))


def normalize_employee_id(raw: str) -> str:
    """'ytr-009', 'YTR 9' -> 'YTR009'. Plain numbers stay ambiguous and are returned as typed."""
    s = (raw or "").strip().upper().replace("-", "").replace(" ", "")
    m = re.match(r"^(YTOP|YTR)(\d{1,3})$", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):03d}"
    return s


def next_free_employee_id(db: Session, group: str) -> str:
    prefix = "YTOP" if group == "Operator" else "YTR"
    ids = db.scalars(select(Employee.employee_id).where(Employee.employee_id.like(f"{prefix}%"))).all()
    nums = [int(i[len(prefix):]) for i in ids if i[len(prefix):].isdigit()]
    # Sample range (YTR801+, YTOP951+) never decides the next real number.
    real = [n for n in nums if not (prefix == "YTR" and n >= 800) and not (prefix == "YTOP" and n >= 950)]
    return f"{prefix}{(max(real) if real else 0) + 1:03d}"


def audit(
    db: Session,
    user: Optional[User],
    action: str,
    entity: str | None = None,
    entity_id: Any = None,
    summary: str | None = None,
    changes: dict | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_label=user.label if user else "system",
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            summary=(summary or "")[:400],
            changes=changes,
            ip=ip,
        )
    )


def diff(old: dict, new: dict) -> dict:
    out = {}
    for k, v in new.items():
        ov = old.get(k)
        if _norm(ov) != _norm(v):
            out[k] = [_jsonable(ov), _jsonable(v)]
    return out


def _norm(v):
    if v == "":
        return None
    return v


def _jsonable(v):
    if isinstance(v, date):
        return v.isoformat()
    return v


# ---------- scope: which org units a user may see ----------


def descendants(db: Session, codes: Iterable[str]) -> set[str]:
    units = db.scalars(select(OrgUnit)).all()
    children: dict[str, list[str]] = {}
    for u in units:
        if u.parent_code:
            children.setdefault(u.parent_code, []).append(u.code)
    out: set[str] = set()
    stack = list(codes)
    while stack:
        c = stack.pop()
        if c in out:
            continue
        out.add(c)
        stack.extend(children.get(c, []))
    return out


@dataclass
class Scope:
    all: bool = False
    org_units: set[str] = field(default_factory=set)
    employee_id: Optional[str] = None

    def allows_unit(self, code: Optional[str]) -> bool:
        return self.all or (code is not None and code in self.org_units)


def user_scope(db: Session, user: User) -> Scope:
    if user.has("admin", "hr_manager", "hr", "gm", "auditor"):
        return Scope(all=True, employee_id=user.employee_id)
    roots: set[str] = {s.org_unit_code for s in user.scopes}
    if user.employee_id:
        roots |= set(
            db.scalars(select(OrgUnit.code).where(OrgUnit.head_employee_id == user.employee_id)).all()
        )
    return Scope(all=False, org_units=descendants(db, roots), employee_id=user.employee_id)
