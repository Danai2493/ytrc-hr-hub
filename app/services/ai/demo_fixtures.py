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
    if form_type == "LEAVE_FORM":
        # a sick-leave form written in Thai: dates readable, ID partly smudged
        start = date.today() - timedelta(days=(date.today().weekday() - 1) % 7 or 7)  # last Tuesday
        eid = hint.get("employee_id", "YTOP958")
        return {
            "employee_id": f(eid, 0.91),
            "name": f(hint.get("names", {}).get(eid, "Wichai H. (SAMPLE)"), 0.88),
            "department": f("Drying & Packing", 0.9),
            "leave_type": f("SICK", 0.95),
            "start_date": f(start.isoformat(), 0.93),
            "end_date": f((start + timedelta(days=2)).isoformat(), 0.62),  # the 2 could be a 3
            "part": f("full", 0.97),
            "days": f(3, 0.9),
            "reason": f("ไข้หวัด มีไข้สูง (SAMPLE)", 0.85),
            "medical_certificate": f(False, 0.8),
            "employee_signed": f(True, 0.95),
            "approved_by_signed": f(True, 0.9),
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


def demo_classify(hint: dict) -> dict:
    """Exit interview coding for the demo: a few keyword rules stand in for the model."""
    if hint.get("task") == "jd":
        return demo_jd(hint)
    if hint.get("task") == "idp":
        from ..growth import ACTION_LIBRARY

        return {"items": [{"competency": c, "goal": f"Use {c} skills every week, seen and confirmed by my supervisor (SAMPLE)",
                           "actions": [{"type": t, "text": x + " (AI SAMPLE)"} for t, x in ACTION_LIBRARY.get(c, [])]} for c in hint.get("focus") or []],
                "note": "Give short feedback every week and check progress after one month. (SAMPLE)"}
    iv = hint.get("interview") or {}
    text = " ".join(str(iv.get(k) or "") for k in ("main_reason", "comments")).lower()
    scores = iv.get("scores") or {}
    rules = [
        ("RES-PAY", ("pay", "salary", "money", "เงิน", "ค่าแรง", "လစာ")),
        ("RES-WORK", ("night", "shift", "tired", "กะ", "เหนื่อย", "ดึก")),
        ("RES-BOSS", ("supervisor", "boss", "หัวหน้า", "shout", "ดุ")),
        ("RES-FAMILY", ("family", "home", "mother", "ครอบครัว", "บ้าน", "မိသားစု")),
        ("RES-HEALTH", ("health", "sick", "ป่วย", "สุขภาพ")),
        ("RES-CAREER", ("study", "career", "เรียน", "งานใหม่")),
    ]
    for code, words in rules:
        hits = [w for w in words if w in text]
        if hits:
            return {"reason_code": code, "confidence": 0.82, "summary_en": f"The employee mainly mentions {hits[0]}-related reasons. Scores are lowest for {min(scores, key=scores.get) if scores else 'no area'}. (SAMPLE)", "themes": hits[:3]}
    low = min(scores, key=scores.get) if scores else None
    code = {"pay": "RES-PAY", "shift": "RES-WORK", "boss": "RES-BOSS", "growth": "RES-CAREER"}.get(low, "RES-OTHER")
    return {"reason_code": code, "confidence": 0.55, "summary_en": "No clear reason in the comments; the lowest score points to this area. (SAMPLE)", "themes": [low] if low else []}


def demo_jd(hint: dict) -> dict:
    """A plausible job description draft for the demo (no model call)."""
    title = hint.get("title") or "Operator"
    unit = hint.get("unit") or "the line"
    skills = hint.get("skills") or []
    return {
        "purpose": f"Runs the {unit} work safely, to the quality standard and the shift plan, so the factory ships on time. (SAMPLE draft)",
        "responsibilities": [
            f"Prepare and run the {unit} equipment at the start of the shift, following the work instruction.",
            "Check the product against the quality standard and stop the line when a defect is found.",
            "Follow safety rules, wear PPE and lock out machines before cleaning or repair.",
            "Record output, downtime and problems on the shift sheet.",
            "Keep the work area clean (5S) and hand over clearly to the next shift.",
        ],
        "education": "Primary school (Grade 6) or higher; reads simple Thai or Myanmar work instructions",
        "experience": "None required; on-the-job training is given",
        "skills": [{"code": c, "level": 3} for c in skills[:6]],
        "notes": f"Draft for {title}. HR and the Department Head check every line before saving.",
    }
