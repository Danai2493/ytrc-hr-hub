"""Starter KPI library for a block rubber (STR 20) factory. A DRAFT for the
Department Heads to correct: names, units and how each is measured."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import KpiDefinition

# code, name_en, name_th, unit, direction, perspective, owner unit, data source, how measured
KPIS = [
    ("KPI-SAF-01", "Lost-time injuries", "จำนวนอุบัติเหตุถึงขั้นหยุดงาน", "cases", "down", "safety", "SAF", "manual", "Injuries with one or more lost work days, from the safety log"),
    ("KPI-SAF-02", "Near-miss reports", "จำนวนรายงานเหตุการณ์เกือบเกิดอุบัติเหตุ", "reports", "up", "safety", "SAF", "manual", "Near-miss cards handed in and logged"),
    ("KPI-SAF-03", "5S audit score", "คะแนนตรวจ 5ส", "%", "up", "safety", "SAF", "manual", "Average of the monthly 5S audits"),
    ("KPI-QA-01", "STR 20 lots passing first test", "ล็อต STR 20 ผ่านการทดสอบครั้งแรก", "%", "up", "quality", "QA", "manual", "Lots passing dirt, ash, volatile matter, PRI and Po on the first lab test"),
    ("KPI-QA-02", "Customer complaints", "ข้อร้องเรียนจากลูกค้า", "cases", "down", "quality", "QA", "manual", "Written complaints accepted by QA"),
    ("KPI-QA-03", "Lab results on time", "ผลแล็บออกตรงเวลา", "%", "up", "quality", "LAB", "manual", "Lot results released within the agreed hours"),
    ("KPI-PRD-01", "Output vs plan", "ผลผลิตเทียบแผน", "%", "up", "delivery", "PRD", "manual", "Tons packed / tons planned"),
    ("KPI-PRD-02", "Dryer downtime (unplanned)", "เวลาหยุดเครื่องอบโดยไม่ได้วางแผน", "hours", "down", "delivery", "PRD", "manual", "Hours the dryer line stopped for breakdowns or no material"),
    ("KPI-PRD-03", "Energy per ton", "พลังงานต่อตัน", "kWh/ton", "down", "cost", "PRD", "manual", "Electricity kWh / tons packed"),
    ("KPI-PRD-04", "Dryer fuel per ton", "เชื้อเพลิงเครื่องอบต่อตัน", "litres/ton", "down", "cost", "PRD", "manual", "Fuel used by the dryers / tons packed"),
    ("KPI-PRD-05", "Bale weight within tolerance", "น้ำหนักก้อนยางอยู่ในเกณฑ์", "%", "up", "quality", "PRD", "manual", "Bales within the weight tolerance at recheck"),
    ("KPI-SHP-01", "Shipments on time", "ส่งมอบตรงเวลา", "%", "up", "delivery", "SGP", "manual", "Containers loaded on the booked date"),
    ("KPI-RMR-01", "Raw material weighing variance", "ผลต่างการชั่งวัตถุดิบ", "%", "down", "cost", "RMR", "manual", "Difference between receiving weight and supplier weight"),
    ("KPI-RMP-01", "EUDR traceable supply", "วัตถุดิบตรวจสอบย้อนกลับได้ตาม EUDR", "%", "up", "quality", "RMP", "manual", "Tons with plot-level traceability data"),
    ("KPI-MTN-01", "Preventive maintenance done on time", "ซ่อมบำรุงเชิงป้องกันตรงเวลา", "%", "up", "delivery", "MTN", "manual", "PM jobs done in their week / PM jobs planned"),
    ("KPI-MTN-02", "Breakdown repair time", "เวลาซ่อมเฉลี่ยเมื่อเครื่องเสีย", "hours", "down", "delivery", "MTN", "manual", "Average hours from breakdown to restart"),
    ("KPI-HR-01", "Attendance rate", "อัตราการมาทำงาน", "%", "up", "people", "HR", "attendance_rate", "Worked days / planned work days, from face scans"),
    ("KPI-HR-02", "Yearly turnover", "อัตราการลาออกต่อปี", "%", "down", "people", "HR", "turnover", "Leavers / average headcount"),
    ("KPI-HR-03", "Training hours per person", "ชั่วโมงอบรมต่อคน", "hours", "up", "people", "HR", "training_hours", "Training hours recorded / headcount"),
    ("KPI-HR-04", "Critical skills covered", "ทักษะสำคัญมีคนพร้อมเพียงพอ", "%", "up", "people", "HR", "skill_coverage", "Critical skills with enough people at U or O"),
    ("KPI-HR-05", "OT hours per person per month", "ชั่วโมง OT ต่อคนต่อเดือน", "hours", "down", "cost", "HR", "ot_hours", "Approved OT hours / headcount / months"),
    ("KPI-HR-06", "Days to fill a vacancy", "จำนวนวันในการสรรหา", "days", "down", "people", "HR", "manual", "From request approval to hire, average"),
    ("KPI-AF-01", "Month-end close on time", "ปิดบัญชีสิ้นเดือนตรงเวลา", "%", "up", "delivery", "AF", "manual", "Months closed by the agreed working day"),
    ("KPI-GEN-01", "Improvement ideas put in place", "ข้อเสนอปรับปรุงงานที่นำไปใช้", "ideas", "up", "cost", None, "manual", "Kaizen ideas tested and kept"),
    ("KPI-GEN-02", "Project milestones on time", "งานโครงการเสร็จตามกำหนด", "%", "up", "delivery", None, "manual", "Milestones done by their date / milestones due"),
]


def seed_kpis(db: Session) -> None:
    for code, en, th, unit, direction, persp, owner, src, how in KPIS:
        if db.get(KpiDefinition, code) is None:
            db.add(KpiDefinition(code=code, name_en=en, name_th=th, unit=unit, direction=direction, perspective=persp, owner_unit=owner,
                                 data_source=src, how_measured=how))
    db.flush()
