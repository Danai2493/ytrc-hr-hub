"""Reference data that is not personal: org structure (SP-HRM-01 Rev.22),
shifts (announcement YTRC2026/003), calendars, grades, pay settings.
Department Heads are left empty here; HR sets them in Org & positions."""
from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Calendar, Grade, Holiday, Job, OrgUnit, Shift

# code, name_en, level, parent, calendar, default shift
ORG = [
    ("MGT", "Management", "division", None, "OFFICE", "OFFICE"),
    ("MGT-D", "Management", "department", "MGT", "OFFICE", "OFFICE"),
    ("QAD", "Quality Assurance", "division", None, "FACTORY", "PRD-D"),
    ("QA", "Quality Assurance", "department", "QAD", "FACTORY", "PRD-D"),
    ("LAB", "Laboratory", "department", "QAD", "FACTORY", "PRD-D"),
    ("ADM", "Administration", "division", None, "OFFICE", "OFFICE"),
    ("SGP", "Shipping & General Purchasing", "department", "ADM", "OFFICE", "OFFICE"),
    ("RMP", "Raw Material Purchasing & EUDR", "department", "ADM", "OFFICE", "OFFICE"),
    ("CSR", "CSR", "department", "ADM", "OFFICE", "OFFICE"),
    ("GA", "General Affair", "department", "ADM", "OFFICE", "OFFICE"),
    ("AF", "Accounting & Finance", "department", "ADM", "OFFICE", "OFFICE"),
    ("IT", "Information Technology", "department", "ADM", "OFFICE", "OFFICE"),
    ("HR", "Human Resource", "department", "ADM", "OFFICE", "OFFICE"),
    ("FAC", "Factory", "division", None, "FACTORY", "PRD-D"),
    ("PI", "Project Improvement", "department", "FAC", "OFFICE", "OFFICE"),
    ("PRD", "Production", "department", "FAC", "FACTORY", "PRD-D"),
    ("PRD-PCL", "Pre Cleaning", "section", "PRD", "FACTORY", "PRD-D"),
    ("PRD-MIX", "Mixing & Support", "section", "PRD", "FACTORY", "PRD-D"),
    ("PRD-DRY", "Drying & Packing", "section", "PRD", "FACTORY", "DRY-D"),
    ("PRD-TRL", "Trolley Cleaning & General", "section", "PRD", "FACTORY", "PRD-D"),
    ("PRD-WH", "Warehouse & Recheck", "section", "PRD", "FACTORY", "PRD-D"),
    ("MTN", "Maintenance", "department", "FAC", "FACTORY", "MTN-D"),
    ("SAF", "Safety", "department", "FAC", "OFFICE", "OFFICE"),
    ("ENV", "Environmental", "department", "FAC", "OFFICE", "OFFICE"),
    # RRC is R/M Receiving (Danai, 2026-10-29). It follows the Office calendar.
    ("RMR", "R/M Receiving (RRC)", "department", "FAC", "OFFICE", "RRC-D"),
]

TH_NAMES = {
    "PRD": "ฝ่ายผลิต", "QA": "ประกันคุณภาพ", "LAB": "ห้องปฏิบัติการ", "HR": "ทรัพยากรบุคคล", "GA": "ธุรการ",
    "AF": "บัญชีและการเงิน", "MTN": "ซ่อมบำรุง", "RMR": "รับวัตถุดิบ (RRC)", "SAF": "ความปลอดภัย", "ENV": "สิ่งแวดล้อม", "IT": "เทคโนโลยีสารสนเทศ",
    "PRD-PCL": "ล้างวัตถุดิบ", "PRD-MIX": "ผสม", "PRD-DRY": "อบและบรรจุ", "PRD-TRL": "ล้างรถเข็นและงานทั่วไป", "PRD-WH": "คลังสินค้าและตรวจซ้ำ",
}

# code, name, start, end, break minutes. Break on 8h shifts is a guess: confirm with Production.
SHIFTS = [
    ("OFFICE", "Office 08:00-17:00", time(8), time(17), 60),
    ("PRD-D", "Factory day 07:00-16:00", time(7), time(16), 60),
    ("PRD-N", "Factory night 22:00-07:00", time(22), time(7), 60),
    ("DRY-D", "Dryer & Packing day 07:00-15:00", time(7), time(15), 0),
    ("DRY-N", "Dryer & Packing night 23:00-07:00", time(23), time(7), 0),
    ("RRC-D", "R/M Receiving (RRC) day 08:00-17:00", time(8), time(17), 60),
    ("RRC-N", "R/M Receiving (RRC) night 22:00-07:00", time(22), time(7), 60),
    ("MTN-D", "Maintenance day 08:00-17:00", time(8), time(17), 60),
    ("MTN-N", "Maintenance night 22:00-07:00", time(22), time(7), 60),
    ("MTN-FRI", "Maintenance / Support Friday 07:00-16:00", time(7), time(16), 60),
]

GRADES = ["MD", "VP", "GM", "DGM", "MG", "Ass MG", "S-Expert", "S-SV", "SV", "S-Staff 2", "S-Staff 1", "Staff 2", "Staff 1", "Leader", "Technician 2", "Technician", "Operator", "Gardener"]

JOBS = [
    ("GM", "General Manager", "ผู้จัดการทั่วไป", "Management", "GM"),
    ("MGR-DEPT", "Department Manager", "ผู้จัดการแผนก", "Management", "MG"),
    ("HR-OFF", "HR Officer", "เจ้าหน้าที่บุคคล", "HR", "Staff 2"),
    ("ACC-OFF", "Accounting Officer", "เจ้าหน้าที่บัญชี", "Finance", "Staff 2"),
    ("QA-INSP", "QA Inspector", "พนักงานตรวจสอบคุณภาพ", "Quality", "Staff 1"),
    ("LAB-TECH", "Laboratory Technician", "เจ้าหน้าที่ห้องแล็บ", "Quality", "Technician"),
    ("PRD-SV", "Production Supervisor", "หัวหน้างานผลิต", "Production", "SV"),
    ("PRD-LDR", "Line Leader", "หัวหน้าไลน์", "Production", "Leader"),
    ("MTN-TECH", "Maintenance Technician", "ช่างซ่อมบำรุง", "Maintenance", "Technician 2"),
    ("SAF-OFF", "Safety Officer (jor-por-wor)", "เจ้าหน้าที่ความปลอดภัย", "Safety", "Staff 2"),
    ("OPR-PCL", "Operator, Pre Cleaning", "พนักงานล้างวัตถุดิบ", "Production", "Operator"),
    ("OPR-MIX", "Operator, Mixing", "พนักงานผสม", "Production", "Operator"),
    ("OPR-DRY", "Operator, Drying & Packing", "พนักงานอบและบรรจุ", "Production", "Operator"),
    ("OPR-TRL", "Operator, Trolley & General", "พนักงานทั่วไป", "Production", "Operator"),
    ("OPR-WH", "Operator, Warehouse", "พนักงานคลังสินค้า", "Production", "Operator"),
    ("OPR-RRC", "Operator, R/M Receiving (RRC)", "พนักงานรับวัตถุดิบ (RRC)", "Production", "Operator"),
]

# Company holiday calendars from HR's calendar workbooks (Draf Calendar 2026 and
# 2027 .xlsx), read by services.calendar_import. Later years: Admin > Holidays >
# upload the workbook.
COMPANY_CALENDAR = Path(__file__).with_name("company_calendar.json")
# Weekly rest days, Monday = 0: Office rests on Sunday, Factory on Friday.
CALENDARS = (("OFFICE", "Office calendar", "6"), ("FACTORY", "Factory calendar", "4"))


def seed_reference(db: Session) -> None:
    for code, name, start, end, brk in SHIFTS:
        if not db.get(Shift, code):
            db.add(Shift(code=code, name=name, start=start, end=end, break_minutes=brk))
    for code, name, rest in CALENDARS:
        if not db.get(Calendar, code):
            db.add(Calendar(code=code, name=name, rest_weekdays=rest))
    db.flush()
    for i, (code, name, level, parent, cal, shift) in enumerate(ORG):
        if not db.get(OrgUnit, code):
            db.add(OrgUnit(code=code, name_en=name, name_th=TH_NAMES.get(code), level=level, parent_code=parent, calendar_code=cal, default_shift_code=shift, sort_order=i))
    for i, g in enumerate(GRADES):
        if not db.get(Grade, g):
            db.add(Grade(code=g, name=g, rank=len(GRADES) - i))
    db.flush()
    from ..services.grading import DEFAULT_MIN_POINTS  # DRAFT points ladder, HR Manager adjusts it
    for g in GRADES:
        gr = db.get(Grade, g)
        if gr.min_points is None and g in DEFAULT_MIN_POINTS:
            gr.min_points = DEFAULT_MIN_POINTS[g]
    db.flush()
    for code, en, th, fam, grade in JOBS:
        if not db.get(Job, code):
            db.add(Job(code=code, title_en=en, title_th=th, job_family=fam, default_grade_code=grade))
    from sqlalchemy import select

    for cal in ("OFFICE", "FACTORY"):
        have = set(db.scalars(select(Holiday.day).where(Holiday.calendar_code == cal)).all())
        for year in json.loads(COMPANY_CALENDAR.read_text(encoding="utf-8")).values():
            for d, name in year["holidays"].get(cal, []):
                if date.fromisoformat(d) not in have:
                    db.add(Holiday(calendar_code=cal, day=date.fromisoformat(d), name=name))
    db.flush()
    from ..services import leave

    leave.seed_types(db)
    from .skills import seed_skills

    seed_skills(db)
    from .competency import seed_competency

    seed_competency(db)
    from .kpi import seed_kpis

    seed_kpis(db)
