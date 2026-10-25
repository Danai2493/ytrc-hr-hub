"""OT requests: create with checks, approval hooks, HR verification."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ApprovalRequest, Employee, OTRequest, User
from . import approvals
from .approvals import StepSpec
from .common import audit, next_ref
from .facescan import apply_scan_check
from .timecalc import Finding, check_ot, combine_ot_times, shift_for


@dataclass
class OTResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    request: Optional[OTRequest] = None
    day_kind: str = "workday"
    split: dict = field(default_factory=dict)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "block"]


def preview(
    db: Session, emp: Optional[Employee], d: date, t_from: time, t_to: time, override: bool = False
) -> OTResult:
    if emp is None:
        return OTResult(False, [Finding("emp_unknown", "block", {})])
    shift = shift_for(db, emp, d)
    f, t = combine_ot_times(d, t_from, t_to, shift)
    findings, kind, split = check_ot(db, emp, d, f, t, override=override)
    return OTResult(not any(x.level == "block" for x in findings), findings, None, kind, split)


def create(
    db: Session,
    user: User,
    emp: Employee,
    d: date,
    t_from: time,
    t_to: time,
    reason: str = "",
    source: str = "web",
    override_note: Optional[str] = None,
    capture_line_id: Optional[int] = None,
) -> OTResult:
    """Validate and save one OT line, then send it for approval."""
    override = bool(override_note) and user.has("gm", "hr_manager")
    shift = shift_for(db, emp, d)
    f, t = combine_ot_times(d, t_from, t_to, shift)
    findings, kind, split = check_ot(db, emp, d, f, t, override=override)
    if any(x.level == "block" for x in findings):
        return OTResult(False, findings, None, kind, split)
    req = OTRequest(
        ref=next_ref(db, "OT", d.year),
        employee_id=emp.employee_id,
        work_date=d,
        ot_from=f,
        ot_to=t,
        reason=(reason or "")[:300],
        source=source,
        capture_line_id=capture_line_id,
        pre_approved=d >= date.today(),
        day_kind=kind,
        requested_minutes=sum(split.values()),
        split={"req": split},
        flags=[x.as_dict() for x in findings],
        override_note=override_note if override else None,
        submitted_by=user.label,
        status="pending",
    )
    req.employee = emp
    db.add(req)
    db.flush()
    steps = [StepSpec("dept_head", head_of=emp.org_unit_code)]
    if any(x.code == "weekly_limit" for x in findings):
        steps.append(StepSpec("gm_override", role="gm"))
    steps.append(StepSpec("hr_verify", role="hr"))
    appr = approvals.submit(
        db,
        request_type="OT",
        entity="ot_request",
        entity_id=str(req.id),
        title=f"OT {emp.employee_id} {d.isoformat()} {f:%H:%M}-{t:%H:%M} ({req.requested_hours}h)",
        steps=steps,
        user=user,
        org_unit_code=emp.org_unit_code,
        employee_id=emp.employee_id,
    )
    req.approval_id = appr.id
    apply_scan_check(db, req)
    audit(db, user, "create", "ot_request", req.ref, appr.title)
    return OTResult(True, findings, req, kind, split)


@approvals.on_step("OT")
def _ot_step(db: Session, appr: ApprovalRequest, step, user: User) -> None:
    req = db.get(OTRequest, int(appr.entity_id))
    if req is None:
        return
    if step.name == "dept_head" and req.status == "pending":
        req.status = "approved"
    if step.name == "hr_verify":
        apply_scan_check(db, req)
        req.status = "verified"


@approvals.on_close("OT")
def _ot_close(db: Session, appr: ApprovalRequest, outcome: str, user: User) -> None:
    req = db.get(OTRequest, int(appr.entity_id))
    if req is None:
        return
    if outcome == "rejected":
        req.status = "rejected"
    elif outcome == "cancelled":
        req.status = "cancelled"
    elif outcome == "approved":
        req.status = "verified"


def verify_many(db: Session, user: User, ids: list[int]) -> tuple[int, list[str]]:
    """HR verifies approved OT lines in bulk after the face scan import."""
    done, errors = 0, []
    for rid in ids:
        req = db.get(OTRequest, rid)
        if req is None or req.approval_id is None:
            continue
        appr = db.get(ApprovalRequest, req.approval_id)
        try:
            approvals.decide(db, appr, user, "approved", "Verified against face scan")
            done += 1
        except approvals.ApprovalError as e:
            errors.append(f"{req.ref}: {e}")
    return done, errors
