"""One approval engine for every request type (blueprint section 5).

A workflow is a list of steps. Each step names who approves: the Department
Head of an org unit (looked up from the org chart, walking up when a unit has no
head or the head is the person who submitted) or a role (hr, gm). Every decision
is written to approval_step and the audit log, which replaces paper signatures."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApprovalRequest, ApprovalStep, Employee, OrgUnit, User
from .common import audit


@dataclass
class StepSpec:
    name: str
    role: Optional[str] = None  # approve by role
    head_of: Optional[str] = None  # approve by Department Head of this org unit code


class ApprovalError(Exception):
    pass


# request_type -> callable(db, request, decision, user) run when the request is closed
_ON_CLOSE: dict[str, Callable] = {}
# request_type -> callable(db, request, step, user) run after each approved step
_ON_STEP: dict[str, Callable] = {}


def on_close(request_type: str):
    def deco(fn):
        _ON_CLOSE[request_type] = fn
        return fn

    return deco


def on_step(request_type: str):
    def deco(fn):
        _ON_STEP[request_type] = fn
        return fn

    return deco


def resolve_head(db: Session, org_unit_code: Optional[str], exclude: Optional[str]) -> Optional[str]:
    code = org_unit_code
    seen = set()
    while code and code not in seen:
        seen.add(code)
        unit = db.get(OrgUnit, code)
        if unit is None:
            return None
        if unit.head_employee_id and unit.head_employee_id != exclude:
            head = db.get(Employee, unit.head_employee_id)
            if head and head.status == "Active":
                return head.employee_id
        code = unit.parent_code
    return None


def submit(
    db: Session,
    *,
    request_type: str,
    entity: str,
    entity_id: str,
    title: str,
    steps: list[StepSpec],
    user: Optional[User],
    org_unit_code: Optional[str] = None,
    employee_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        request_type=request_type,
        entity=entity,
        entity_id=str(entity_id),
        title=title[:200],
        org_unit_code=org_unit_code,
        employee_id=employee_id,
        payload=payload,
        submitted_by_user_id=user.id if user else None,
        submitted_by=user.label if user else "system",
    )
    me = user.employee_id if user else None
    seq = 0
    last: tuple | None = None
    for spec in steps:
        approver = role = None
        if spec.head_of is not None:
            approver = resolve_head(db, spec.head_of, None)
            if approver is not None and approver == me:
                continue  # the head is the person submitting: submitting is their approval
            if approver is None:
                role = "gm"  # nobody found up the tree: GM approves
        else:
            role = spec.role
        if (approver, role) == last:
            continue  # same approver twice in a row (e.g. both units have one head)
        seq += 1
        req.steps.append(ApprovalStep(seq=seq, name=spec.name, approver_employee_id=approver, approver_role=role))
        last = (approver, role)
    if not req.steps:
        raise ApprovalError("Workflow has no steps")
    db.add(req)
    db.flush()
    audit(db, user, "submit", "approval", req.id, f"{request_type}: {title}")
    return req


def can_decide(user: User, req: ApprovalRequest, step: Optional[ApprovalStep] = None) -> bool:
    step = step or req.active_step
    if step is None or req.status != "pending" or step.decision is not None:
        return False
    if user.id is not None and user.id == req.submitted_by_user_id:
        return False  # never approve your own request
    if req.employee_id and user.employee_id == req.employee_id:
        return False  # never approve a request about yourself
    if step.approver_employee_id:
        return user.employee_id == step.approver_employee_id or user.has("gm") and step.name == "dept_head"
    if step.approver_role:
        if step.approver_role == "hr":
            return user.has("hr", "hr_manager")
        return user.has(step.approver_role)
    return False


def decide(
    db: Session, req: ApprovalRequest, user: User, decision: str, comment: Optional[str] = None
) -> ApprovalRequest:
    if decision not in ("approved", "rejected"):
        raise ApprovalError("Decision must be approved or rejected")
    step = req.active_step
    if not can_decide(user, req, step):
        raise ApprovalError("You cannot decide this step")
    if decision == "rejected" and not (comment or "").strip():
        raise ApprovalError("Please give a reason when you reject")
    step.decision = decision
    step.decided_by = user.label
    step.decided_at = datetime.now()
    step.comment = (comment or "")[:500] or None
    audit(db, user, decision, "approval", req.id, f"{req.request_type} step {step.seq} {step.name}: {decision}")
    if decision == "rejected":
        req.status = "rejected"
        req.closed_at = datetime.now()
        _close(db, req, "rejected", user)
        return req
    if step.name and req.request_type in _ON_STEP:
        _ON_STEP[req.request_type](db, req, step, user)
    if step.seq >= max(s.seq for s in req.steps):
        req.status = "approved"
        req.closed_at = datetime.now()
        _close(db, req, "approved", user)
    else:
        req.current_step = step.seq + 1
    db.flush()
    return req


def record_paper_signature(db: Session, req: ApprovalRequest, user: User, note: str, step_name: str = "dept_head") -> bool:
    """A Department Head who already signed the paper form should not have to
    approve again on screen. HR records that signature as the first step; the
    request then moves on (or closes) like any other decision."""
    step = req.active_step
    if step is None or req.status != "pending" or step.name != step_name:
        return False
    step.decision = "approved"
    step.decided_by = f"paper signature, recorded by {user.label}"
    step.decided_at = datetime.now()
    step.comment = note[:500]
    audit(db, user, "approved", "approval", req.id, f"Paper signature recorded: {note}")
    if req.request_type in _ON_STEP:
        _ON_STEP[req.request_type](db, req, step, user)
    if step.seq >= max(s.seq for s in req.steps):
        req.status = "approved"
        req.closed_at = datetime.now()
        _close(db, req, "approved", user)
    else:
        req.current_step = step.seq + 1
    db.flush()
    return True


def cancel(db: Session, req: ApprovalRequest, user: User, reason: str) -> None:
    if req.status != "pending":
        raise ApprovalError("Only pending requests can be cancelled")
    if user.id != req.submitted_by_user_id and not user.has("hr_manager"):
        raise ApprovalError("Only the submitter or the HR Manager can cancel")
    req.status = "cancelled"
    req.closed_at = datetime.now()
    audit(db, user, "cancel", "approval", req.id, reason)
    _close(db, req, "cancelled", user)


def _close(db: Session, req: ApprovalRequest, outcome: str, user: User) -> None:
    fn = _ON_CLOSE.get(req.request_type)
    if fn:
        fn(db, req, outcome, user)


def inbox(db: Session, user: User) -> list[ApprovalRequest]:
    pending = db.scalars(
        select(ApprovalRequest).where(ApprovalRequest.status == "pending").order_by(ApprovalRequest.created_at)
    ).all()
    return [r for r in pending if can_decide(user, r)]
