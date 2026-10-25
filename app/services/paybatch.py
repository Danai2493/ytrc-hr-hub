"""Pay input batch for Bplus (M3). The app sends checked hours per person and
pay code; Bplus applies rates and keeps salaries. Bplus sends back totals by
org unit only."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Employee, OTRequest, PayBatch, PayBatchLine, PayTotal, User
from .common import audit, get_setting
from .timecalc import cycle_for

CYCLES = {
    "STAFF_MONTHLY": "Staff and managers, monthly (OT 21st to 20th)",
    "OPERATOR_HALF": "Operators, twice a month (1-15, 16-end)",
}


class BatchError(Exception):
    pass


def build(db: Session, user: User, cycle: str, start: date, end: date) -> PayBatch:
    if cycle not in CYCLES:
        raise BatchError("Unknown pay cycle")
    open_batch = db.scalar(
        select(PayBatch).where(
            PayBatch.cycle == cycle, PayBatch.period_start == start, PayBatch.status.in_(("draft", "checked"))
        )
    )
    if open_batch:
        raise BatchError(f"Batch #{open_batch.id} for this period is still open. Rebuild or send it first.")
    n = (
        db.scalar(
            select(func.max(PayBatch.batch_no)).where(PayBatch.cycle == cycle, PayBatch.period_start == start)
        )
        or 0
    )
    batch = PayBatch(cycle=cycle, period_start=start, period_end=end, batch_no=n + 1, created_by=user.label)
    db.add(batch)
    db.flush()
    fill(db, batch)
    audit(db, user, "create", "pay_batch", batch.id, f"{cycle} {start}..{end} #{batch.batch_no}")
    return batch


def fill(db: Session, batch: PayBatch) -> None:
    for ln in list(batch.lines):
        db.delete(ln)
    db.flush()
    for r in db.scalars(select(OTRequest).where(OTRequest.pay_batch_id == batch.id)):
        r.pay_batch_id = None
    reqs = db.scalars(
        select(OTRequest).where(
            OTRequest.work_date >= batch.period_start,
            OTRequest.work_date <= batch.period_end,
            OTRequest.status == "verified",
            OTRequest.pay_batch_id.is_(None),
        )
    ).all()
    sums: dict[tuple[str, str], float] = defaultdict(float)
    refs: dict[tuple[str, str], list[str]] = defaultdict(list)
    units: dict[str, str] = {}
    pending_scan: list[str] = []
    for r in reqs:
        emp = r.employee
        if cycle_for(emp) != batch.cycle:
            continue
        pay = (r.split or {}).get("pay")
        if r.scan_minutes is None or pay is None:
            pending_scan.append(r.ref)
            continue
        for code, minutes in pay.items():
            if minutes:
                sums[(emp.employee_id, code)] += minutes / 60
                refs[(emp.employee_id, code)].append(r.ref)
        units[emp.employee_id] = emp.org_unit_code
        r.pay_batch_id = batch.id
    for (eid, code), hours in sorted(sums.items()):
        batch.lines.append(
            PayBatchLine(
                employee_id=eid,
                org_unit_code=units.get(eid),
                pay_code=code,
                quantity=round(hours, 2),
                source_refs=refs[(eid, code)],
            )
        )
    db.flush()
    flags = variance_flags(db, batch)
    if pending_scan:
        flags.insert(0, {"level": "flag", "msg": f"{len(pending_scan)} verified OT lines have no face scan check yet and were left out", "refs": pending_scan[:30]})
    batch.flags = flags
    batch.totals = totals(batch)
    batch.status = "draft"


def totals(batch: PayBatch) -> dict:
    by_code: dict[str, float] = defaultdict(float)
    people = set()
    for ln in batch.lines:
        by_code[ln.pay_code] += ln.quantity
        people.add(ln.employee_id)
    return {"hours": round(sum(by_code.values()), 2), "by_code": {k: round(v, 2) for k, v in by_code.items()}, "people": len(people)}


def variance_flags(db: Session, batch: PayBatch) -> list[dict]:
    pct = float(get_setting(db, "pay_variance_pct") or 30)
    prev = db.scalar(
        select(PayBatch)
        .where(PayBatch.cycle == batch.cycle, PayBatch.period_start < batch.period_start, PayBatch.status.in_(("sent", "imported")))
        .order_by(PayBatch.period_start.desc())
    )
    if prev is None:
        return [{"level": "info", "msg": "No earlier sent batch to compare with"}]
    def per(b: PayBatch, key) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for ln in b.lines:
            out[key(ln)] += ln.quantity
        return out
    flags = []
    for label, key in (("person", lambda ln: ln.employee_id), ("org unit", lambda ln: ln.org_unit_code or "?")):
        now, before = per(batch, key), per(prev, key)
        for k, h in now.items():
            b = before.get(k, 0)
            if b == 0 and h >= 8:
                flags.append({"level": "flag", "msg": f"{label} {k}: {h:.1f}h OT, none last period"})
            elif b and abs(h - b) / b * 100 > pct:
                flags.append({"level": "flag", "msg": f"{label} {k}: {h:.1f}h vs {b:.1f}h last period ({(h - b) / b * 100:+.0f}%)"})
    return flags


def export_csv(db: Session, batch: PayBatch) -> str:
    cols = get_setting(db, "bplus_export_columns") or ["employee_id", "pay_code", "hours", "period_start", "period_end"]
    code_map = get_setting(db, "bplus_pay_code_map") or {}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for ln in batch.lines:
        row = {
            "employee_id": ln.employee_id,
            "pay_code": code_map.get(ln.pay_code, ln.pay_code),
            "hours": f"{ln.quantity:.2f}",
            "period_start": batch.period_start.strftime("%d/%m/%Y"),
            "period_end": batch.period_end.strftime("%d/%m/%Y"),
            "org_unit": ln.org_unit_code or "",
        }
        w.writerow([row.get(c, "") for c in cols])
    return buf.getvalue()


def mark(db: Session, user: User, batch: PayBatch, status: str, note: Optional[str] = None) -> None:
    order = ["draft", "checked", "sent", "imported"]
    if status not in order or order.index(status) != order.index(batch.status) + 1:
        raise BatchError(f"Cannot move a {batch.status} batch to {status}")
    if status == "checked":
        if batch.created_by == user.label and not user.has("hr_manager"):
            raise BatchError("A second HR person must check the batch")
        batch.checked_by = user.label
    if status == "sent":
        batch.sent_at = datetime.now()
        for r in db.scalars(select(OTRequest).where(OTRequest.pay_batch_id == batch.id)):
            r.status = "paid"
    batch.status = status
    if note:
        batch.bplus_status_note = note[:300]
    audit(db, user, status, "pay_batch", batch.id, note or "")


def import_totals(db: Session, user: User, text: str) -> int:
    """CSV from Bplus: month (YYYY-MM), org_unit, total_pay_cost, ot_cost, headcount_paid, ot_hours"""
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            y, m = row["month"].strip()[:7].split("-")
            month = date(int(y), int(m), 1)
        except (KeyError, ValueError):
            continue
        unit = (row.get("org_unit") or "").strip()
        rec = db.scalar(select(PayTotal).where(PayTotal.month == month, PayTotal.org_unit_code == unit))
        if rec is None:
            rec = PayTotal(month=month, org_unit_code=unit)
            db.add(rec)
        for f in ("total_pay_cost", "ot_cost", "ot_hours"):
            v = (row.get(f) or "").replace(",", "").strip()
            setattr(rec, f, float(v) if v else None)
        hc = (row.get("headcount_paid") or "").strip()
        rec.headcount_paid = int(float(hc)) if hc else None
        n += 1
    audit(db, user, "import", "pay_total", None, f"{n} rows of Bplus totals")
    return n


def reconcile(db: Session, month: date) -> list[dict]:
    """App OT hours vs Bplus OT hours by org unit for one month."""
    out = []
    totals_rows = db.scalars(select(PayTotal).where(PayTotal.month == month)).all()
    if not totals_rows:
        return out
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    app_hours: dict[str, float] = defaultdict(float)
    for r in db.scalars(select(OTRequest).where(OTRequest.work_date >= month, OTRequest.work_date < nxt, OTRequest.status.in_(("verified", "paid")))):
        app_hours[r.employee.org_unit_code] += (r.payable_minutes or 0) / 60
    for t in totals_rows:
        a = round(app_hours.get(t.org_unit_code, 0), 2)
        b = t.ot_hours
        out.append({"org_unit": t.org_unit_code, "app": a, "bplus": b, "ok": b is None or abs(a - b) < 0.5})
    return out
