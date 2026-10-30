"""Paper form capture: photo -> AI reads it -> automatic checks -> HR review queue.

Nothing the AI reads goes into the data on its own. Clean lines are pre-ticked,
unclear lines and discrepancies are highlighted, and an HR person accepts them.
Accepted OT lines become normal OT requests (source = paper) with the same
checks and approval log as web requests."""
from __future__ import annotations

import difflib
import io
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    ApprovalRequest,
    CaptureLine,
    Employee,
    EmployeeRestricted,
    FormCapture,
    User,
)
from ..common import audit, normalize_employee_id
from ..timecalc import check_ot, combine_ot_times, shift_for
from .forms import FORMS
from .providers import AIError, get_provider

KEY_FIELDS = ("employee_id", "ot_from", "ot_to", "hours")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def store_file(data: bytes, mime: str, original: str) -> tuple[str, str]:
    """Save the photo (downscaled, EXIF stripped) under STORAGE_DIR. Returns (relative path, mime)."""
    if mime not in ALLOWED_MIME:
        raise AIError("Please upload a photo (JPG, PNG or WEBP)")
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((2200, 2200))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)  # re-encoding drops EXIF (GPS, phone model)
        data, mime, ext = buf.getvalue(), "image/jpeg", "jpg"
    except Exception:
        ext = mime.split("/")[-1]
    now = datetime.now()
    rel = f"captures/{now:%Y/%m}/{uuid.uuid4().hex}.{ext}"
    path = settings.storage_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel, mime


def file_path(capture: FormCapture) -> Path:
    p = (settings.storage_dir / capture.file_path).resolve()
    if settings.storage_dir not in p.parents:
        raise AIError("Bad file path")
    return p


def upload(db: Session, user: User, form_type: str, data: bytes, mime: str, original: str, hint: Optional[dict] = None) -> FormCapture:
    if form_type not in FORMS:
        raise AIError("Unknown form type")
    rel, mime = store_file(data, mime, original)
    cap = FormCapture(form_type=form_type, file_path=rel, mime=mime, original_name=original[:200], uploaded_by=user.label)
    db.add(cap)
    db.flush()
    audit(db, user, "upload", "form_capture", cap.id, f"{form_type} photo uploaded")
    extract(db, cap, hint)
    return cap


def extract(db: Session, cap: FormCapture, hint: Optional[dict] = None) -> None:
    schema, prompt = FORMS[cap.form_type]
    provider = get_provider()
    hint = dict(hint or {})
    if provider.name == "mock":  # demo answers use the SAMPLE people's names
        hint["names"] = {e.employee_id: e.full_name_en for e in db.scalars(select(Employee).where(Employee.is_sample.is_(True)))}
    try:
        res = provider.extract(file_path(cap).read_bytes(), cap.mime, schema, prompt, {"form_type": cap.form_type, **hint})
    except AIError as e:
        cap.status, cap.error, cap.provider = "failed", str(e), provider.name
        return
    except Exception as e:  # network, bad JSON
        cap.status, cap.error, cap.provider = "failed", f"{type(e).__name__}: {e}"[:1000], provider.name
        return
    cap.provider, cap.model, cap.raw_response = res.provider, res.model, res.data
    cap.error = None
    for ln in list(cap.lines):
        db.delete(ln)
    db.flush()
    if cap.form_type == "OT_FORM":
        _load_ot(db, cap, res.data)
    elif cap.form_type == "LEAVE_FORM":
        _load_leave(db, cap, res.data)
    else:
        _load_permit(db, cap, res.data)
    db.flush()


def _val(x):
    return x.get("value") if isinstance(x, dict) else x


def _conf(x):
    return float(x.get("confidence", 0)) if isinstance(x, dict) else 1.0


def _load_ot(db: Session, cap: FormCapture, data: dict) -> None:
    keys = ("form_code", "work_date", "department", "shift", "requested_by", "approved_by_signed")
    cap.header = {k: _val(data.get(k)) for k in keys}
    cap.header["page_quality"] = data.get("page_quality")
    cap.header_confidence = {k: _conf(data.get(k)) for k in keys}
    for i, row in enumerate(data.get("rows") or [], start=1):
        fields = {k: _val(v) for k, v in row.items()}
        conf = {k: _conf(v) for k, v in row.items()}
        cap.lines.append(CaptureLine(line_no=i, fields=fields, confidence=conf))
    db.flush()
    validate_ot(db, cap)


def parse_hhmm(s) -> Optional[time]:
    if not s:
        return None
    s = str(s).strip().replace(".", ":").replace(" ", "")
    if ":" not in s and s.isdigit() and len(s) in (3, 4):
        s = s[:-2] + ":" + s[-2:]
    try:
        hh, mm = s.split(":")[:2]
        return time(int(hh) % 24, int(mm))
    except (ValueError, TypeError):
        return None


def parse_day(s) -> Optional[date]:
    if not s:
        return None
    try:
        d = date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
    if d.year > 2400:
        d = d.replace(year=d.year - 543)
    return d


def validate_ot(db: Session, cap: FormCapture) -> None:
    """Checks every line. Run again after HR edits a line."""
    thr = settings.ai_review_threshold
    header = cap.header or {}
    work_date = parse_day(header.get("work_date"))
    header_issues = []
    if header.get("page_quality") not in (None, "good"):
        header_issues.append(f"Photo quality: {header.get('page_quality')}")
    if work_date is None:
        header_issues.append("Work date unreadable")
    if (cap.header_confidence or {}).get("work_date", 1) < thr:
        header_issues.append("Work date unclear")
    if not header.get("approved_by_signed"):
        header_issues.append("No approval signature on the form")
    seen: set[str] = set()
    all_ok = not header_issues
    for ln in cap.lines:
        if ln.status in ("accepted", "dropped"):
            continue
        f = ln.fields
        conf = ln.confidence or {}
        issues: list[dict] = []
        low = [k for k in KEY_FIELDS if conf.get(k, 1) < thr]
        ln.min_confidence = min([conf.get(k, 1) for k in KEY_FIELDS] or [1])
        if low:
            issues.append({"code": "low_confidence", "level": "flag", "params": {"fields": ", ".join(low)}})
        eid = normalize_employee_id(f.get("employee_id") or "")
        emp = db.get(Employee, eid) if eid else None
        if emp is None:
            guess = _guess_by_name(db, f.get("name"))
            issues.append({"code": "id_not_found", "level": "block", "params": {"id": f.get("employee_id") or "-", "guess": guess or ""}})
        else:
            f["employee_id"] = eid
            ratio = _name_ratio(f.get("name"), emp)
            if f.get("name") and ratio < 0.55:
                issues.append({"code": "name_mismatch", "level": "flag", "params": {"master": emp.full_name_en, "form": f.get("name")}})
            if eid in seen:
                issues.append({"code": "dup_on_form", "level": "block", "params": {}})
            seen.add(eid)
        t0, t1 = parse_hhmm(f.get("ot_from")), parse_hhmm(f.get("ot_to"))
        if t0 is None or t1 is None:
            issues.append({"code": "time_unreadable", "level": "block", "params": {}})
        if emp is not None and work_date and t0 and t1:
            shift = shift_for(db, emp, work_date)
            a, b = combine_ot_times(work_date, t0, t1, shift)
            findings, kind, split = check_ot(db, emp, work_date, a, b)
            issues += [x.as_dict() for x in findings if x.code != "after_the_fact"]
            minutes = sum(split.values())
            written = f.get("hours")
            span = (b - a).total_seconds() / 3600
            if written not in (None, ""):
                try:
                    w = float(written)
                    if abs(w - span) > 0.01:
                        issues.append({"code": "hours_mismatch", "level": "flag", "params": {"written": w, "times": round(span, 2)}})
                    elif abs(w - minutes / 60) > 0.01:
                        issues.append({"code": "hours_not_payable", "level": "flag", "params": {"written": w, "payable": round(minutes / 60, 2)}})
                except ValueError:
                    issues.append({"code": "hours_unreadable", "level": "flag", "params": {}})
            ln.fields = {**f, "_minutes": minutes, "_day_kind": kind}
        else:
            ln.fields = dict(f)
        if f.get("signed") is False:
            issues.append({"code": "not_signed", "level": "flag", "params": {}})
        ln.issues = issues
        ln.status = "ok" if not issues else "review"
        all_ok = all_ok and not issues
    cap.header = {**header, "_issues": header_issues}
    if cap.status not in ("approved", "rejected"):
        cap.status = "extracted" if all_ok else "needs_review"


def _name_ratio(form_name: Optional[str], emp: Employee) -> float:
    if not form_name:
        return 1.0
    a = form_name.lower().replace("(sample)", "").strip()
    best = 0.0
    for n in (emp.full_name_en, emp.full_name_th, emp.full_name_my, emp.nickname):
        if n:
            b = n.lower().replace("(sample)", "").strip()
            best = max(best, difflib.SequenceMatcher(None, a, b).ratio(), 1.0 if a and (a in b or b in a) else 0)
    return best


def _guess_by_name(db: Session, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    best, best_id = 0.0, None
    for emp in db.scalars(select(Employee).where(Employee.status == "Active")):
        r = _name_ratio(name, emp)
        if r > best:
            best, best_id = r, emp.employee_id
    return best_id if best >= 0.75 else None


def update_line(db: Session, user: User, cap: FormCapture, line: CaptureLine, values: dict) -> None:
    before = dict(line.fields)
    f = dict(line.fields)
    for k in ("employee_id", "name", "ot_from", "ot_to", "hours"):
        if k in values:
            f[k] = values[k] or None
    line.fields = f
    conf = dict(line.confidence or {})
    for k in values:
        conf[k] = 1.0  # a person checked it
    line.confidence = conf
    line.status = "review"
    audit(db, user, "edit", "capture_line", line.id, f"HR corrected line {line.line_no}", {"before": before, "after": f})
    validate_ot(db, cap)


def accept_ot(db: Session, user: User, cap: FormCapture, line_ids: list[int], paper_signed: bool, reason: str = "") -> tuple[int, list[str]]:
    """Turn accepted lines into OT requests. If the department head signed the
    paper form, that signature is recorded as the first approval step."""
    from .. import ot as ot_service

    if not user.has("hr", "hr_manager"):
        raise AIError("Only HR can accept captured forms")
    work_date = parse_day((cap.header or {}).get("work_date"))
    if work_date is None:
        raise AIError("Set the work date first")
    made, errors = 0, []
    for ln in cap.lines:
        if ln.id not in line_ids or ln.status in ("accepted", "dropped"):
            continue
        if any(i["level"] == "block" for i in (ln.issues or [])):
            errors.append(f"Line {ln.line_no}: fix the blocking issue first")
            continue
        emp = db.get(Employee, ln.fields.get("employee_id"))
        t0, t1 = parse_hhmm(ln.fields.get("ot_from")), parse_hhmm(ln.fields.get("ot_to"))
        res = ot_service.create(db, user, emp, work_date, t0, t1, reason or f"Paper form FM-HRM-25 (capture #{cap.id})", source="paper", capture_line_id=ln.id)
        if not res.ok:
            errors.append(f"Line {ln.line_no}: " + ", ".join(b.code for b in res.blocks))
            continue
        ln.status = "accepted"
        ln.ot_request_id = res.request.id
        made += 1
        if paper_signed and res.request.approval_id:
            appr = db.get(ApprovalRequest, res.request.approval_id)
            step = appr.active_step
            if step and step.name == "dept_head":
                step.decision = "approved"
                step.decided_by = f"paper signature, recorded by {user.label}"
                step.decided_at = datetime.now()
                step.comment = f"Signed on paper form, photo capture #{cap.id}"
                appr.current_step = step.seq + 1
                res.request.status = "approved"
                audit(db, user, "approved", "approval", appr.id, f"Paper signature recorded from capture #{cap.id}")
    if all(ln.status in ("accepted", "dropped") for ln in cap.lines):
        cap.status = "approved"
        cap.reviewed_by = user.label
        cap.reviewed_at = datetime.now()
    audit(db, user, "accept", "form_capture", cap.id, f"{made} OT lines accepted")
    return made, errors


def drop_line(db: Session, user: User, cap: FormCapture, line: CaptureLine, note: str) -> None:
    line.status = "dropped"
    line.issues = (line.issues or []) + [{"code": "dropped", "level": "info", "params": {"note": note}}]
    audit(db, user, "drop", "capture_line", line.id, note)
    if all(ln.status in ("accepted", "dropped") for ln in cap.lines):
        cap.status = "approved"


# ---------- work permit ----------


def _load_permit(db: Session, cap: FormCapture, data: dict) -> None:
    keys = ("full_name", "nationality", "passport_no", "work_permit_no", "issue_date", "expiry_date", "employer")
    cap.header = {k: _val(data.get(k)) for k in keys}
    cap.header["page_quality"] = data.get("page_quality")
    cap.header_confidence = {k: _conf(data.get(k)) for k in keys}
    validate_permit(db, cap)


def validate_permit(db: Session, cap: FormCapture) -> None:
    h = cap.header or {}
    thr = settings.ai_review_threshold
    issues = []
    low = [k for k, c in (cap.header_confidence or {}).items() if c < thr and k in ("work_permit_no", "expiry_date", "passport_no")]
    if low:
        issues.append(f"Unclear: {', '.join(low)}")
    exp = parse_day(h.get("expiry_date"))
    if exp is None:
        issues.append("Expiry date unreadable")
    elif exp < date.today():
        issues.append(f"Permit already expired on {exp}")
    emp = db.get(Employee, cap.employee_id) if cap.employee_id else None
    if emp is None:
        guess = _guess_by_name(db, h.get("full_name"))
        if guess:
            cap.employee_id = guess
            emp = db.get(Employee, guess)
            issues.append(f"Matched to {guess} by name: please confirm")
        else:
            issues.append("Could not match the name to an employee")
    elif h.get("full_name") and _name_ratio(h.get("full_name"), emp) < 0.55:
        issues.append(f"Name on permit differs from master ({emp.full_name_en})")
    if emp and emp.restricted and emp.restricted.work_permit_no and h.get("work_permit_no") and emp.restricted.work_permit_no != h.get("work_permit_no"):
        issues.append(f"Replaces permit {emp.restricted.work_permit_no} on file")
    cap.header = {**h, "_issues": issues}
    if cap.status not in ("approved", "rejected"):
        cap.status = "needs_review" if issues else "extracted"


def accept_permit(db: Session, user: User, cap: FormCapture, employee_id: str, values: dict) -> None:
    if not user.has("hr", "hr_manager"):
        raise AIError("Only HR can accept captured forms")
    emp = db.get(Employee, normalize_employee_id(employee_id))
    if emp is None:
        raise AIError("Employee not found")
    exp = parse_day(values.get("expiry_date"))
    if exp is None or not values.get("work_permit_no"):
        raise AIError("Work permit number and expiry date are required")
    r = emp.restricted or EmployeeRestricted(employee_id=emp.employee_id)
    before = {"work_permit_no": r.work_permit_no, "work_permit_expiry": r.work_permit_expiry.isoformat() if r.work_permit_expiry else None, "passport_no": r.passport_no}
    r.work_permit_no = values["work_permit_no"].strip()
    r.work_permit_expiry = exp
    if values.get("passport_no"):
        r.passport_no = values["passport_no"].strip()
    db.add(r)
    cap.employee_id = emp.employee_id
    cap.status = "approved"
    cap.reviewed_by = user.label
    cap.reviewed_at = datetime.now()
    audit(db, user, "update_restricted", "employee", emp.employee_id, f"Work permit from capture #{cap.id}", {"before": before, "after": {"work_permit_no": r.work_permit_no, "work_permit_expiry": exp.isoformat()}})


# ---------- leave form ----------

LEAVE_KEYS = ("employee_id", "name", "department", "leave_type", "start_date", "end_date", "part", "days", "reason",
              "medical_certificate", "employee_signed", "approved_by_signed")


def _load_leave(db: Session, cap: FormCapture, data: dict) -> None:
    cap.header = {k: _val(data.get(k)) for k in LEAVE_KEYS}
    cap.header["page_quality"] = data.get("page_quality")
    cap.header_confidence = {k: _conf(data.get(k)) for k in LEAVE_KEYS}
    validate_leave(db, cap)


def validate_leave(db: Session, cap: FormCapture) -> None:
    """Same checks as a leave typed on screen, plus what only paper has:
    unclear handwriting, a days total that does not match the dates, no signature."""
    from ...models import LeaveType
    from .. import leave as leave_service

    h = {k: v for k, v in (cap.header or {}).items() if not k.startswith("_")}
    conf = cap.header_confidence or {}
    thr = settings.ai_review_threshold
    findings: list[dict] = []
    issues: list[str] = []
    if h.get("page_quality") not in (None, "good"):
        issues.append(f"Photo quality: {h.get('page_quality')}")
    low = [k for k in ("employee_id", "leave_type", "start_date", "end_date") if conf.get(k, 1) < thr]
    if low:
        findings.append({"code": "low_confidence", "level": "flag", "params": {"fields": ", ".join(low)}})
    eid = normalize_employee_id(h.get("employee_id") or "") or cap.employee_id
    emp = db.get(Employee, eid) if eid else None
    if emp is None:
        guess = _guess_by_name(db, h.get("name"))
        findings.append({"code": "id_not_found", "level": "block", "params": {"id": h.get("employee_id") or "-", "guess": guess or ""}})
    else:
        h["employee_id"] = cap.employee_id = emp.employee_id
        if h.get("name") and _name_ratio(h.get("name"), emp) < 0.55:
            findings.append({"code": "name_mismatch", "level": "flag", "params": {"master": emp.full_name_en, "form": h.get("name")}})
    lt = db.get(LeaveType, h.get("leave_type") or "")
    d0, d1 = parse_day(h.get("start_date")), parse_day(h.get("end_date"))
    if lt is None:
        issues.append("Leave type unreadable")
    if d0 is None or d1 is None:
        issues.append("Leave dates unreadable")
    if not h.get("employee_signed"):
        findings.append({"code": "not_signed", "level": "flag", "params": {}})
    if not h.get("approved_by_signed"):
        issues.append("No approval signature on the form")
    if emp is not None and lt is not None and d0 and d1:
        part = h.get("part") if h.get("part") in ("am", "pm") else "full"
        res, days = leave_service.check(db, emp, lt, d0, d1, part, bool(h.get("medical_certificate")))
        findings += [x.as_dict() for x in res if x.code != "leave_after"]
        h["_days"] = days
        written = h.get("days")
        if written not in (None, ""):
            try:
                if abs(float(written) - days) > 0.01:
                    findings.append({"code": "leave_days_mismatch", "level": "flag", "params": {"written": float(written), "days": days}})
            except (TypeError, ValueError):
                pass
    cap.header = {**h, "_issues": issues, "_findings": findings}
    if cap.status not in ("approved", "rejected"):
        cap.status = "extracted" if not issues and not findings else "needs_review"


def update_leave(db: Session, user: User, cap: FormCapture, values: dict) -> None:
    before = {k: v for k, v in (cap.header or {}).items() if not k.startswith("_")}
    h = dict(before)
    conf = dict(cap.header_confidence or {})
    for k in ("employee_id", "leave_type", "start_date", "end_date", "part", "reason"):
        if k in values and str(values[k] or "") != str(h.get(k) or ""):
            h[k] = values[k] or None
            conf[k] = 1.0  # a person checked it
    for k in ("medical_certificate", "approved_by_signed"):
        h[k] = bool(values.get(k))
    cap.header, cap.header_confidence = h, conf
    audit(db, user, "edit", "form_capture", cap.id, "HR corrected the leave form", {"before": before, "after": h})
    validate_leave(db, cap)


def accept_leave(db: Session, user: User, cap: FormCapture, paper_signed: bool):
    from .. import approvals
    from .. import leave as leave_service

    if not user.has("hr", "hr_manager"):
        raise AIError("Only HR can accept captured forms")
    h = cap.header or {}
    if any(f.get("level") == "block" for f in h.get("_findings") or []) or h.get("_issues") and any("unreadable" in i for i in h["_issues"]):
        raise AIError("Fix the blocking issue first")
    emp = db.get(Employee, cap.employee_id or "")
    d0, d1 = parse_day(h.get("start_date")), parse_day(h.get("end_date"))
    if emp is None or d0 is None or d1 is None:
        raise AIError("Employee and dates are required")
    part = h.get("part") if h.get("part") in ("am", "pm") else "full"
    res = leave_service.create(db, user, emp, h.get("leave_type") or "", d0, d1, part, h.get("reason") or f"Paper leave form (capture #{cap.id})",
                               has_document=bool(h.get("medical_certificate")), source="paper", capture_id=cap.id)
    if not res.ok:
        raise AIError("; ".join(f.code for f in res.findings if f.level == "block"))
    if paper_signed and res.request.approval_id:
        appr = db.get(ApprovalRequest, res.request.approval_id)
        approvals.record_paper_signature(db, appr, user, f"Signed on paper leave form, photo capture #{cap.id}")
    cap.status = "approved"
    cap.reviewed_by = user.label
    cap.reviewed_at = datetime.now()
    audit(db, user, "accept", "form_capture", cap.id, f"Leave {res.request.ref} created from paper form")
    return res.request
