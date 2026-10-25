"""Answers the mock provider gives in the demo. They copy real patterns found in
step 3 (an OT row written 06:30-08:00 when the night shift ends at 07:00, a
hard-to-read ID) but use SAMPLE people only."""
from __future__ import annotations

from datetime import date, timedelta


def f(value, confidence=0.97):
    return {"value": value, "confidence": confidence}


def demo_answer(form_type: str, hint: dict) -> dict:
    if form_type == "WORK_PERMIT":
        return {
            "full_name": f(hint.get("name", "MG AUNG KYAW (SAMPLE)"), 0.93),
            "nationality": f("Myanmar", 0.99),
            "passport_no": f("MD000000", 0.71),
            "work_permit_no": f("WP-SAMPLE-0001", 0.95),
            "issue_date": f("2026-02-01", 0.92),
            "expiry_date": f("2027-01-31", 0.9),
            "employer": f("SAMPLE COMPANY", 0.88),
            "page_quality": "good",
        }
    d = hint.get("work_date") or date.today().isoformat()
    names = hint.get("names") or {}
    rows = [
        ("YTOP951", "Somchai A. (SAMPLE)", "17:00", "19:00", 2, 0.98),
        ("YTOP952", "Mya Mya (SAMPLE)", "17:00", "19:00", 2, 0.97),
        ("YTOP953", "Aung Min (SAMPLE)", "17:00", "20:00", 3, 0.96),
        ("YTOP95?", "Kyaw Zin (SAMPLE)", "17:00", "19:00", 2, 0.52),  # unclear last digit
        ("YTOP955", "Nilar (SAMPLE)", "16:30", "19:00", 2, 0.95),  # hours do not match times
        ("YTOP956", "Thida (SAMPLE)", "17:00", "21:00", 4, 0.94),
    ]
    out = []
    for eid, name, t0, t1, h, conf in rows:
        real_id = "YTOP954" if eid == "YTOP95?" else eid
        name = names.get(real_id, name)
        out.append(
            {
                "employee_id": f(eid, conf),
                "name": f(name, 0.9),
                "ot_from": f(t0, 0.95),
                "ot_to": f(t1, 0.95),
                "hours": f(h, 0.93),
                "job": f("Packing", 0.8),
                "signed": f(True, 0.9),
                "note": f(None, 0.9),
            }
        )
    return {
        "form_code": f("FM-HRM-25", 0.99),
        "work_date": f(d, 0.96),
        "department": f(hint.get("department", "Drying & Packing"), 0.9),
        "shift": f("Day", 0.85),
        "requested_by": f("Supervisor (SAMPLE)", 0.8),
        "approved_by_signed": f(True, 0.9),
        "rows": out,
        "page_quality": "good",
    }
