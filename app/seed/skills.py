"""Starter skill and course list for a block rubber (STR) factory. A DRAFT for
HR and the Department Heads to correct: names, levels and minimum U/O numbers
are a senior HR guess from the org chart, not company data."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Course, Job, JobSkill, Skill

# code, English, Thai, category, unit, critical, min U/O, course, valid months
SKILLS = [
    ("SK-RRC-01", "Raw rubber receiving and grading", "รับและคัดเกรดยางวัตถุดิบ", "Process", "RMR", True, 3, "CR-RRC-01", None),
    ("SK-RRC-02", "Truck scale and weighing records", "ชั่งน้ำหนักและบันทึก", "Process", "RMR", False, 2, None, None),
    ("SK-PCL-01", "Pre-cleaning line (cutter, crusher)", "ไลน์ล้างวัตถุดิบ (เครื่องตัด เครื่องบด)", "Machine", "PRD-PCL", True, 4, "CR-PCL-01", None),
    ("SK-PCL-02", "Creper and hammer mill", "เครื่องเครป และแฮมเมอร์มิลล์", "Machine", "PRD-PCL", True, 3, None, None),
    ("SK-MIX-01", "Blending and mixing to grade", "ผสมยางตามเกรด", "Process", "PRD-MIX", True, 2, None, None),
    ("SK-DRY-01", "Dryer operation and temperature control", "ควบคุมเตาอบและอุณหภูมิ", "Machine", "PRD-DRY", True, 4, "CR-DRY-01", None),
    ("SK-DRY-02", "Trolley loading", "จัดยางลงรถเข็น", "Process", "PRD-DRY", False, 4, None, None),
    ("SK-DRY-03", "Bale press and weighing", "อัดก้อนและชั่งน้ำหนัก", "Machine", "PRD-DRY", True, 3, None, None),
    ("SK-DRY-04", "Metal detection and packing", "ตรวจโลหะและบรรจุ", "Quality", "PRD-DRY", True, 3, None, None),
    ("SK-WH-01", "Pallet, label and stock recheck", "จัดพาเลท ติดฉลาก และตรวจนับ", "Process", "PRD-WH", False, 2, None, None),
    ("SK-FLT-01", "Forklift driving (licence)", "ขับรถยก (มีใบอนุญาต)", "Legal licence", "PRD-WH", True, 3, "CR-FLT-01", 24),
    ("SK-QA-01", "Visual quality check and sampling", "ตรวจคุณภาพด้วยสายตาและเก็บตัวอย่าง", "Quality", "QA", False, 2, None, None),
    ("SK-LAB-01", "Lab tests: dirt, ash, Po, PRI", "ทดสอบแล็บ: สิ่งสกปรก เถ้า Po PRI", "Quality", "LAB", True, 2, None, None),
    ("SK-SAF-01", "Safety rules and PPE", "กฎความปลอดภัยและอุปกรณ์ PPE", "Safety", None, False, 0, "CR-SAF-01", None),
    ("SK-SAF-02", "Lock-out tag-out", "การตัดพลังงานและแขวนป้าย (LOTO)", "Safety", None, True, 0, "CR-SAF-02", 12),
    ("SK-SAF-03", "Basic fire fighting", "การดับเพลิงขั้นต้น", "Safety", None, False, 0, "CR-SAF-03", 12),
    ("SK-5S-01", "5S and housekeeping", "5ส", "Process", None, False, 0, None, None),
    ("SK-MTN-01", "Electrical maintenance", "ซ่อมบำรุงไฟฟ้า", "Machine", "MTN", True, 2, None, None),
    ("SK-LDR-01", "Leading a shift team", "การนำทีมในกะ", "Leadership", None, False, 0, "CR-LDR-01", None),
]

# code, English, Thai, kind, hours, legal, valid months, skill, level reached, induction
COURSES = [
    ("CR-IND-01", "Company induction and rules", "ปฐมนิเทศและระเบียบบริษัท", "internal", 3, False, None, None, None, True),
    ("CR-SAF-01", "Safety induction for new staff", "อบรมความปลอดภัยพนักงานใหม่", "internal", 6, True, None, "SK-SAF-01", 2, True),
    ("CR-SAF-02", "Lock-out tag-out", "การตัดพลังงานและแขวนป้าย", "internal", 3, True, 12, "SK-SAF-02", 3, False),
    ("CR-SAF-03", "Basic fire fighting and evacuation drill", "ดับเพลิงขั้นต้นและซ้อมอพยพ", "external", 6, True, 12, "SK-SAF-03", 3, False),
    ("CR-FLT-01", "Forklift driver training", "อบรมผู้ขับรถยก", "external", 12, True, 24, "SK-FLT-01", 3, False),
    ("CR-RRC-01", "Raw rubber grading OJT", "OJT คัดเกรดยาง", "OJT", 16, False, None, "SK-RRC-01", 2, False),
    ("CR-PCL-01", "Pre-cleaning line OJT", "OJT ไลน์ล้างวัตถุดิบ", "OJT", 16, False, None, "SK-PCL-01", 2, False),
    ("CR-DRY-01", "Dryer control OJT", "OJT ควบคุมเตาอบ", "OJT", 24, False, None, "SK-DRY-01", 2, False),
    ("CR-LDR-01", "Supervisor basics: OT rules, leave, safety talk", "พื้นฐานหัวหน้างาน: กฎ OT การลา ทอล์กความปลอดภัย", "internal", 6, False, None, "SK-LDR-01", 2, False),
]

# job, [(skill, level)]
JOB_SKILLS = {
    "OPR-RRC": [("SK-RRC-01", 3), ("SK-RRC-02", 3), ("SK-SAF-01", 3), ("SK-5S-01", 2)],
    "OPR-PCL": [("SK-PCL-01", 3), ("SK-PCL-02", 2), ("SK-SAF-01", 3), ("SK-SAF-02", 3), ("SK-5S-01", 2)],
    "OPR-MIX": [("SK-MIX-01", 3), ("SK-SAF-01", 3), ("SK-5S-01", 2)],
    "OPR-DRY": [("SK-DRY-01", 3), ("SK-DRY-02", 3), ("SK-DRY-03", 3), ("SK-DRY-04", 2), ("SK-SAF-01", 3), ("SK-SAF-02", 3), ("SK-5S-01", 2)],
    "OPR-WH": [("SK-WH-01", 3), ("SK-FLT-01", 3), ("SK-SAF-01", 3), ("SK-5S-01", 2)],
    "OPR-TRL": [("SK-DRY-02", 2), ("SK-SAF-01", 3), ("SK-5S-01", 3)],
    "PRD-SV": [("SK-LDR-01", 3), ("SK-SAF-01", 4), ("SK-SAF-02", 3), ("SK-SAF-03", 3)],
    "PRD-LDR": [("SK-LDR-01", 3), ("SK-SAF-01", 3), ("SK-SAF-02", 3)],
    "QA-INSP": [("SK-QA-01", 3), ("SK-SAF-01", 3)],
    "LAB-TECH": [("SK-LAB-01", 3), ("SK-SAF-01", 3)],
    "MTN-TECH": [("SK-MTN-01", 3), ("SK-SAF-01", 3), ("SK-SAF-02", 4)],
    "SAF-OFF": [("SK-SAF-01", 4), ("SK-SAF-02", 4), ("SK-SAF-03", 4)],
}


def seed_skills(db: Session) -> None:
    for i, (code, en, th, cat, unit, crit, min_uo, course, valid) in enumerate(SKILLS):
        if db.get(Skill, code) is None:
            db.add(Skill(code=code, name_en=en, name_th=th, category=cat, org_unit_code=unit, critical=crit, min_uo=min_uo,
                         course_code=course, valid_months=valid, sort_order=i))
    db.flush()
    for code, en, th, kind, hours, legal, valid, skill, lvl, ind in COURSES:
        if db.get(Course, code) is None:
            db.add(Course(code=code, name_en=en, name_th=th, kind=kind, hours=hours, legal=legal, valid_months=valid,
                          skill_code=skill, skill_level=lvl, for_new_staff=ind))
    db.flush()
    for job, rows in JOB_SKILLS.items():
        if db.get(Job, job) is None:
            continue
        for skill, lvl in rows:
            if db.get(JobSkill, (job, skill)) is None:
                db.add(JobSkill(job_code=job, skill_code=skill, level=lvl))
    db.flush()
