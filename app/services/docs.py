"""Personnel documents with expiry, and the policy library with acknowledgement."""
from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Employee, EmployeeDocument, EmployeeRestricted, Policy, PolicyAck, User
from ..models.docs import ACK_METHODS, APPLIES_TO, DOC_TYPES
from .common import audit, get_setting

MAX_FILE = 10 * 1024 * 1024
MIME_OK = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
HR = ("hr", "hr_manager")


class DocError(ValueError):
    pass


def alert_days(db: Session) -> int:
    try:
        return int(get_setting(db, "doc_alert_days") or 60)
    except (TypeError, ValueError):
        return 60


# ---------------- files ----------------

def store(data: bytes, mime: str, folder: str) -> tuple[str, str]:
    if mime not in MIME_OK:
        raise DocError("Please upload a PDF or a photo (JPG, PNG)")
    if len(data) > MAX_FILE:
        raise DocError("The file is over 10 MB")
    ext = MIME_OK[mime]
    if mime != "application/pdf":
        try:  # re-encode photos: drops EXIF (GPS, phone model)
            from PIL import Image, ImageOps

            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            img.thumbnail((2400, 2400))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            data, mime, ext = buf.getvalue(), "image/jpeg", "jpg"
        except Exception as e:  # not a real image
            raise DocError("The photo could not be opened") from e
    now = datetime.now()
    rel = f"{folder}/{now:%Y/%m}/{uuid.uuid4().hex}.{ext}"
    p = settings.storage_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel, mime


def path_of(rel: Optional[str]) -> Optional[Path]:
    if not rel:
        return None
    base = settings.storage_dir.resolve()
    p = (base / rel).resolve()
    return p if base in p.parents and p.exists() else None


# ---------------- personnel documents ----------------

def can_see_docs(user: User, emp: Employee) -> bool:
    return user.has(*HR) or user.employee_id == emp.employee_id


def can_open_file(user: User, emp: Employee) -> bool:
    """Scans: HR only. The employee sees that a document is on file, not the scan (it may be a copy HR made)."""
    return user.has(*HR)


def needed_types(emp: Employee) -> list[str]:
    out = []
    for code, (_, _, who) in DOC_TYPES.items():
        if who == "all" or (who == "foreign" and emp.is_foreign_worker) or (who == "thai" and emp.nationality == "Thai"):
            out.append(code)
    return out


def documents(db: Session, emp_id: str, include_old: bool = False) -> list[EmployeeDocument]:
    q = select(EmployeeDocument).where(EmployeeDocument.employee_id == emp_id)
    if not include_old:
        q = q.where(EmployeeDocument.current.is_(True))
    return list(db.scalars(q.order_by(EmployeeDocument.doc_type, EmployeeDocument.issued_on.desc())))


def state(doc: EmployeeDocument, today: date, days: int) -> str:
    if doc.expires_on is None:
        return "ok"
    if doc.expires_on < today:
        return "expired"
    if doc.expires_on <= today + timedelta(days=days):
        return "soon"
    return "ok"


def add_document(db: Session, user: User, emp: Employee, form, data: bytes = b"", mime: str = "", name: str = "") -> EmployeeDocument:
    if not user.has(*HR):
        raise DocError("Only HR adds documents")
    t = form.get("doc_type")
    if t not in DOC_TYPES:
        raise DocError("Choose the document type")
    paper = bool(form.get("paper_only"))
    if not data and not paper:
        raise DocError("Attach the scan, or tick paper only")

    def d(k):
        v = (form.get(k) or "").strip()
        return date.fromisoformat(v) if v else None

    issued, expires = d("issued_on"), d("expires_on")
    if DOC_TYPES[t][1] and t not in ("certificate", "other", "medical") and not expires:
        raise DocError("This document expires: enter the expiry date")
    if issued and expires and expires <= issued:
        raise DocError("The expiry date must be after the issue date")
    num = (form.get("number") or "").strip().replace(" ", "").replace("-", "")
    doc = EmployeeDocument(employee_id=emp.employee_id, doc_type=t, title=(form.get("title") or "").strip()[:160] or None,
                           number_tail=num[-4:] or None, issued_on=issued, expires_on=expires, paper_only=paper,
                           note=(form.get("note") or "").strip()[:300] or None, uploaded_by=user.label)
    if data:
        doc.file_path, doc.file_mime = store(data, mime, "docs")
        doc.file_name = (name or "")[:160]
    # one current copy per type (certificates and "other" can have many)
    if t not in ("certificate", "other", "education"):
        for old in db.scalars(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.employee_id, EmployeeDocument.doc_type == t,
                                                              EmployeeDocument.current.is_(True))):
            old.current = False
    db.add(doc)
    # enter once: the work permit expiry also drives the legal checks
    if t == "work_permit" and expires:
        r = db.get(EmployeeRestricted, emp.employee_id) or EmployeeRestricted(employee_id=emp.employee_id)
        r.work_permit_expiry = expires
        db.add(r)
    if t == "visa" and expires:
        r = db.get(EmployeeRestricted, emp.employee_id) or EmployeeRestricted(employee_id=emp.employee_id)
        r.visa_expiry = expires
        db.add(r)
    db.flush()
    audit(db, user, "doc_add", "employee", emp.employee_id, f"{t} added{' (paper only)' if paper else ''}")
    return doc


def remove_document(db: Session, user: User, doc: EmployeeDocument) -> None:
    if not user.has("hr_manager"):
        raise DocError("Only the HR Manager removes a document")
    p = path_of(doc.file_path)
    if p:
        p.unlink()
    audit(db, user, "doc_remove", "employee", doc.employee_id, f"{doc.doc_type} removed")
    db.delete(doc)


@dataclass
class DocRow:
    employee: Employee
    doc_type: str
    problem: str  # expired / soon / missing
    doc: Optional[EmployeeDocument] = None


def watchlist(db: Session, today: Optional[date] = None) -> list[DocRow]:
    today = today or date.today()
    days = alert_days(db)
    out = []
    emps = list(db.scalars(select(Employee).where(Employee.status == "Active").order_by(Employee.employee_id)))
    cur: dict[tuple[str, str], list[EmployeeDocument]] = {}
    for doc in db.scalars(select(EmployeeDocument).where(EmployeeDocument.current.is_(True))):
        cur.setdefault((doc.employee_id, doc.doc_type), []).append(doc)
    for e in emps:
        for t in needed_types(e):
            if (e.employee_id, t) not in cur:
                out.append(DocRow(e, t, "missing"))
        for (eid, t), docs in cur.items():
            if eid != e.employee_id:
                continue
            for doc in docs:
                s = state(doc, today, days)
                if s != "ok":
                    out.append(DocRow(e, t, s, doc))
    order = {"expired": 0, "soon": 1, "missing": 2}
    return sorted(out, key=lambda r: (order[r.problem], r.doc.expires_on if r.doc and r.doc.expires_on else date.max, r.employee.employee_id))


# ---------------- policies ----------------

def audience(db: Session, p: Policy) -> list[Employee]:
    q = select(Employee).where(Employee.status == "Active")
    if p.applies_to != "all":
        q = q.where(Employee.employee_group == p.applies_to)
    return list(db.scalars(q.order_by(Employee.employee_id)))


def applies(p: Policy, e: Employee) -> bool:
    return p.applies_to == "all" or e.employee_group == p.applies_to


def published(db: Session) -> list[Policy]:
    return list(db.scalars(select(Policy).where(Policy.status == "published").order_by(Policy.code)))


def ack_due(p: Policy) -> date:
    base = max(p.effective_on, p.published_at.date() if p.published_at else p.effective_on)
    return base + timedelta(days=p.ack_days or 14)


def progress(db: Session, p: Policy) -> dict:
    people = audience(db, p)
    done = {a.employee_id for a in p.acks}
    n = len(people)
    k = sum(1 for e in people if e.employee_id in done)
    return {"people": n, "done": k, "pct": round(100 * k / n) if n else 0, "missing": [e for e in people if e.employee_id not in done]}


def my_todo(db: Session, user: User) -> list[Policy]:
    if not user.employee_id:
        return []
    e = db.get(Employee, user.employee_id)
    if e is None:
        return []
    done = set(db.scalars(select(PolicyAck.policy_id).where(PolicyAck.employee_id == e.employee_id)))
    return [p for p in published(db) if p.ack_required and applies(p, e) and p.id not in done]


def save_policy(db: Session, user: User, p: Optional[Policy], form, data: bytes = b"", mime: str = "") -> Policy:
    if not user.has(*HR):
        raise DocError("Only HR edits policies")
    if p is not None and p.status != "draft":
        raise DocError("A published policy cannot change: make a new version")
    code = (form.get("code") or "").strip().upper()
    title = (form.get("title_en") or "").strip()
    if not code or not title:
        raise DocError("Code and title are needed")
    eff = (form.get("effective_on") or "").strip()
    if p is None:
        last = db.scalar(select(Policy).where(Policy.code == code).order_by(Policy.version.desc()))
        if last and last.status == "draft":
            raise DocError("A draft of this policy already exists")
        p = Policy(code=code, version=(last.version + 1) if last else 1)
        db.add(p)
    p.title_en, p.title_th = title[:160], (form.get("title_th") or "").strip()[:160] or None
    p.summary = (form.get("summary") or "").strip() or None
    p.changes = (form.get("changes") or "").strip() or None
    p.applies_to = form.get("applies_to") if form.get("applies_to") in APPLIES_TO else "all"
    p.effective_on = date.fromisoformat(eff) if eff else date.today()
    p.ack_required = bool(form.get("ack_required"))
    try:
        p.ack_days = max(1, min(90, int(form.get("ack_days") or 14)))
    except ValueError:
        p.ack_days = 14
    if data:
        p.file_path, p.file_mime = store(data, mime, "policies")
    db.flush()
    audit(db, user, "policy_save", "policy", f"{p.code} v{p.version}", "draft saved")
    return p


def new_version(db: Session, user: User, p: Policy) -> Policy:
    if not user.has(*HR):
        raise DocError("Only HR edits policies")
    if db.scalar(select(Policy).where(Policy.code == p.code, Policy.status == "draft")):
        raise DocError("A draft of this policy already exists")
    n = Policy(code=p.code, version=p.version + 1, title_en=p.title_en, title_th=p.title_th, summary=p.summary, applies_to=p.applies_to,
               effective_on=date.today(), ack_required=p.ack_required, ack_days=p.ack_days, file_path=p.file_path, file_mime=p.file_mime)
    db.add(n)
    db.flush()
    return n


def publish(db: Session, user: User, p: Policy) -> None:
    if not user.has("hr_manager"):
        raise DocError("The HR Manager publishes policies")
    if p.status != "draft":
        raise DocError("Already published")
    if not p.summary or not p.file_path:
        raise DocError("Add the key points and the policy file before publishing")
    for old in db.scalars(select(Policy).where(Policy.code == p.code, Policy.status == "published")):
        old.status = "retired"
    p.status, p.published_by, p.published_at = "published", user.label, datetime.now()
    audit(db, user, "policy_publish", "policy", f"{p.code} v{p.version}", p.title_en)


def acknowledge(db: Session, user: User, p: Policy) -> None:
    if p.status != "published":
        raise DocError("This version is not in force")
    e = db.get(Employee, user.employee_id) if user.employee_id else None
    if e is None or not applies(p, e):
        raise DocError("This policy is not for you")
    if db.scalar(select(PolicyAck).where(PolicyAck.policy_id == p.id, PolicyAck.employee_id == e.employee_id)):
        return
    db.add(PolicyAck(policy_id=p.id, employee_id=e.employee_id, on=date.today(), method="web", recorded_by=user.label))
    audit(db, user, "policy_ack", "policy", f"{p.code} v{p.version}", "read and understood")


def record_briefing(db: Session, user: User, p: Policy, emp_ids: list[str], on: date, method: str) -> int:
    """Operators without a login: HR records who signed the briefing sheet."""
    if not user.has(*HR):
        raise DocError("Only HR records a briefing")
    if p.status != "published":
        raise DocError("This version is not in force")
    if method not in ACK_METHODS or method == "web":
        raise DocError("Choose briefing or signed paper")
    if on > date.today():
        raise DocError("The date cannot be in the future")
    done = {a.employee_id for a in p.acks}
    n = 0
    for eid in emp_ids:
        e = db.get(Employee, eid)
        if e is None or not applies(p, e) or eid in done:
            continue
        db.add(PolicyAck(policy_id=p.id, employee_id=eid, on=on, method=method, recorded_by=user.label))
        n += 1
    if n:
        audit(db, user, "policy_briefing", "policy", f"{p.code} v{p.version}", f"{n} people, {method}, {on}")
    return n
