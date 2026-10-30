from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Course, Employee, EmployeeSkill, Job, JobSkill, OrgUnit, Skill, TrainingNeed, TrainingRecord
from ..models.develop import LEVELS, NEED_REASONS, SKILL_CATEGORIES
from ..security import check_csrf, require
from ..services import jobs as J
from ..services import skills as S
from ..services.common import normalize_employee_id, user_scope
from .ot import _d, team_for
from .templating import flash, redirect, render

router = APIRouter(prefix="/skills")
WHO = ("hr", "hr_manager", "gm", "dept_head", "supervisor")
HR = ("hr", "hr_manager")


def _units(db: Session, team: list[Employee]) -> list[OrgUnit]:
    codes = {e.org_unit_code for e in team}
    return [u for u in db.scalars(select(OrgUnit).order_by(OrgUnit.sort_order)) if u.code in codes]


@router.get("")
def matrix_page(request: Request, unit: str = "", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    team = team_for(db, user)
    units = _units(db, team)
    if not unit:
        me = db.get(Employee, user.employee_id) if user.employee_id else None
        unit = next((u.code for u in units if u.code == "PRD-DRY"), None) if user.has("hr", "hr_manager", "gm") else None
        unit = unit or (me.org_unit_code if me and any(u.code == me.org_unit_code for u in units) else (units[0].code if units else ""))
    people = [e for e in team if e.org_unit_code == unit]
    m = S.matrix(db, people)
    return render(request, "skills/matrix.html", {"tab": "matrix", "units": units, "unit": unit, "m": m, "levels": LEVELS,
                                                  "can": S.can_assess(user), "today": date.today()})


@router.post("/set", dependencies=[Depends(check_csrf)])
def set_level(request: Request, employee_id: str = Form(...), skill_code: str = Form(...), level: int = Form(...), note: str = Form(""),
              back: str = Form("/skills"), db: Session = Depends(get_db), user=Depends(require(*WHO))):
    if not S.can_assess(user):
        raise HTTPException(403)
    team = {e.employee_id: e for e in team_for(db, user)}
    emp, skill = team.get(employee_id), db.get(Skill, skill_code)
    if emp is None or skill is None:
        raise HTTPException(404)
    try:
        S.set_level(db, user, emp, skill, level, note)
        db.commit()
        flash(request, f"{emp.employee_id} {skill.code}: {S.letter(level)}")
    except ValueError as e:
        db.rollback()
        flash(request, str(e), "bad")
    if not back.startswith("/skills"):
        back = "/skills"
    return redirect(back)


@router.get("/person/{emp_id}")
def person(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    emp = next((e for e in team_for(db, user) if e.employee_id == emp_id), None)
    if emp is None:
        raise HTTPException(404)
    m = S.matrix(db, [emp])
    held = {r.skill_code: r for r in db.scalars(select(EmployeeSkill).where(EmployeeSkill.employee_id == emp_id))}
    records = db.scalars(select(TrainingRecord).where(TrainingRecord.employee_id == emp_id).order_by(TrainingRecord.on_date.desc())).all()
    needs = db.scalars(select(TrainingNeed).where(TrainingNeed.employee_id == emp_id).order_by(TrainingNeed.status, TrainingNeed.due_date)).all()
    return render(request, "skills/person.html", {"tab": "matrix", "emp": emp, "m": m, "held": held, "records": records, "needs": needs,
                                                  "levels": LEVELS, "reasons": NEED_REASONS, "can": S.can_assess(user)})


# ---------- training needs and records ----------

@router.get("/needs")
def needs(request: Request, unit: str = "", status: str = "open", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    team = {e.employee_id: e for e in team_for(db, user)}
    q = select(TrainingNeed).order_by(TrainingNeed.priority, TrainingNeed.due_date)
    if status:
        q = q.where(TrainingNeed.status == status)
    rows = [n for n in db.scalars(q) if n.employee_id in team and (not unit or team[n.employee_id].org_unit_code == unit)]
    by_course: dict[str, int] = {}
    for n in rows:
        if n.course_code and n.status in ("open", "planned"):
            by_course[n.course_code] = by_course.get(n.course_code, 0) + 1
    courses = {c.code: c for c in db.scalars(select(Course))}
    return render(request, "skills/needs.html", {"tab": "needs", "rows": rows, "units": _units(db, list(team.values())), "unit": unit,
                                                 "status": status, "reasons": NEED_REASONS, "by_course": by_course, "courses": courses,
                                                 "is_hr": user.has(*HR), "today": date.today()})


@router.post("/needs/refresh", dependencies=[Depends(check_csrf)])
def needs_refresh(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    n = S.generate_needs(db)
    db.commit()
    flash(request, f"{n} new training need(s) found")
    return redirect("/skills/needs")


@router.post("/needs/{nid}", dependencies=[Depends(check_csrf)])
def need_update(request: Request, nid: int, status: str = Form(...), note: str = Form(""), db: Session = Depends(get_db), user=Depends(require(*WHO))):
    n = db.get(TrainingNeed, nid)
    team = {e.employee_id for e in team_for(db, user)}
    if n is None or n.employee_id not in team or status not in ("open", "planned", "dropped"):
        raise HTTPException(404)
    if status == "dropped" and not note.strip():
        flash(request, "Say why the need is dropped", "bad")
        return redirect("/skills/needs")
    n.status, n.note = status, (note or n.note or "")[:300] or None
    db.commit()
    return redirect("/skills/needs")


@router.get("/training")
def training(request: Request, course: str = "", d0: str = "", d1: str = "", db: Session = Depends(get_db), user=Depends(require(*WHO))):
    start = _d(d0) or date(date.today().year, 1, 1)
    end = _d(d1) or date.today()
    team = {e.employee_id for e in team_for(db, user)}
    q = select(TrainingRecord).where(TrainingRecord.on_date >= start, TrainingRecord.on_date <= end).order_by(TrainingRecord.on_date.desc(), TrainingRecord.session_ref)
    if course:
        q = q.where(TrainingRecord.course_code == course)
    rows = [r for r in db.scalars(q) if r.employee_id in team]
    sessions: dict[str, dict] = {}
    for r in rows:
        s = sessions.setdefault(r.session_ref or f"#{r.id}", {"ref": r.session_ref, "course": r.course, "on": r.on_date, "trainer": r.trainer, "people": 0, "passed": 0, "hours": 0.0})
        s["people"] += 1
        s["passed"] += r.result == "passed"
        s["hours"] += r.hours
    courses = list(db.scalars(select(Course).where(Course.active.is_(True)).order_by(Course.code)))
    return render(request, "skills/training.html", {"tab": "records", "sessions": list(sessions.values()), "rows": rows, "courses": courses,
                                                    "course": course, "d0": start, "d1": end, "is_hr": user.has(*HR)})


@router.get("/training/new")
def training_new(request: Request, course: str = "", db: Session = Depends(get_db), user=Depends(require(*HR))):
    courses = list(db.scalars(select(Course).where(Course.active.is_(True)).order_by(Course.code)))
    c = db.get(Course, course) if course else None
    suggested = []
    if c:
        suggested = [n.employee_id for n in db.scalars(select(TrainingNeed).where(TrainingNeed.course_code == c.code, TrainingNeed.status.in_(("open", "planned"))))]
    return render(request, "skills/training_new.html", {"tab": "records", "courses": courses, "c": c, "suggested": suggested,
                                                        "team": team_for(db, user), "today": date.today()})


@router.post("/training/new", dependencies=[Depends(check_csrf)])
async def training_save(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    f = await request.form()
    c = db.get(Course, f.get("course") or "")
    on = _d(f.get("on_date"))
    if c is None or on is None:
        flash(request, "Choose the course and the date", "bad")
        return redirect("/skills/training/new")
    ids = []
    for raw in (f.get("people") or "").replace(",", " ").split():
        eid = normalize_employee_id(raw)
        if eid and eid not in ids:
            ids.append(eid)
    ids += [x for x in f.getlist("emp") if x not in ids]
    unknown = [i for i in ids if db.get(Employee, i) is None]
    ids = [i for i in ids if i not in unknown]
    failed = set(f.getlist("failed"))
    default = "passed" if c.skill_code or c.legal else "attended"
    results = {i: ("failed" if i in failed else default) for i in ids}
    try:
        hours = float(f.get("hours")) if f.get("hours") else None
    except ValueError:
        hours = None
    ref, made = S.record_session(db, user, c, on, ids, results, f.get("trainer") or "", hours, f.get("note") or "")
    db.commit()
    flash(request, f"{ref}: {len(made)} people recorded for {c.code}")
    for u in unknown:
        flash(request, f"{u}: not in the employee master", "bad")
    return redirect(f"/skills/training?course={c.code}")


# ---------- jobs and job descriptions ----------

@router.get("/jobs")
def jobs(request: Request, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    rows = list(db.scalars(select(Job).where(Job.active.is_(True)).order_by(Job.job_family, Job.code)))
    n_skills = {}
    for r in db.scalars(select(JobSkill)):
        n_skills[r.job_code] = n_skills.get(r.job_code, 0) + 1
    heads = {}
    for e in db.scalars(select(Employee).where(Employee.status == "Active")):
        heads[e.job_code] = heads.get(e.job_code, 0) + 1
    return render(request, "skills/jobs.html", {"tab": "jobs", "rows": rows, "n_skills": n_skills, "heads": heads})


def _job_ctx(db: Session, job: Job, draft: dict | None = None) -> dict:
    req = {r.skill_code: r.level for r in db.scalars(select(JobSkill).where(JobSkill.job_code == job.code))}
    if draft:
        req = {**req, **{x["code"]: int(x["level"]) for x in draft.get("skills") or []}}  # AI adds or changes; it never drops a skill
    return {"tab": "jobs", "job": job, "req": req, "skills": list(db.scalars(select(Skill).where(Skill.active.is_(True)).order_by(Skill.sort_order))),
            "levels": LEVELS, "units": J.units_for(db, job), "draft": draft}


@router.get("/jobs/{code}")
def job_page(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    job = db.get(Job, code)
    if job is None:
        raise HTTPException(404)
    return render(request, "skills/job.html", {**_job_ctx(db, job), "is_hr": user.has(*HR)})


@router.post("/jobs/{code}/draft", dependencies=[Depends(check_csrf)])
def job_draft(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require(*HR))):
    job = db.get(Job, code)
    if job is None:
        raise HTTPException(404)
    data, err = J.draft(db, job)
    if err:
        flash(request, err, "bad")
        return redirect(f"/skills/jobs/{code}")
    flash(request, "AI draft below. Nothing is saved until you press Save.", "warn")
    return render(request, "skills/job.html", {**_job_ctx(db, job, data), "is_hr": True})


@router.post("/jobs/{code}", dependencies=[Depends(check_csrf)])
async def job_save(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require(*HR))):
    job = db.get(Job, code)
    if job is None:
        raise HTTPException(404)
    f = await request.form()
    skills = {}
    for s in db.scalars(select(Skill)):
        v = f.get(f"lvl_{s.code}")
        if v is not None:
            try:
                skills[s.code] = int(v)
            except ValueError:
                continue
    J.save(db, user, job, f.get("purpose") or "", f.get("responsibilities") or "", f.get("education") or "", f.get("experience") or "", skills)
    S.generate_needs(db)
    db.commit()
    flash(request, "Job description saved")
    return redirect(f"/skills/jobs/{code}")


# ---------- catalog ----------

@router.get("/catalog")
def catalog(request: Request, db: Session = Depends(get_db), user=Depends(require(*WHO))):
    skills = list(db.scalars(select(Skill).order_by(Skill.sort_order)))
    courses = list(db.scalars(select(Course).order_by(Course.code)))
    m = S.matrix(db, list(db.scalars(select(Employee).where(Employee.status == "Active"))))
    units = list(db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.sort_order)))
    return render(request, "skills/catalog.html", {"tab": "catalog", "skills": skills, "courses": courses, "cov": m.coverage,
                                                   "cats": SKILL_CATEGORIES, "units": units, "is_hr": user.has(*HR)})


@router.post("/catalog/skill", dependencies=[Depends(check_csrf)])
async def save_skill(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    f = await request.form()
    code = (f.get("code") or "").strip().upper()
    if not code:
        flash(request, "Code missing or already used", "bad")
        return redirect("/skills/catalog")
    s = db.get(Skill, code)
    if s is None:
        s = Skill(code=code, name_en=f.get("name_en") or code, sort_order=999)
        db.add(s)
    s.name_en = (f.get("name_en") or s.name_en)[:120]
    s.name_th = (f.get("name_th") or s.name_th or "")[:120] or None
    s.category = f.get("category") if f.get("category") in SKILL_CATEGORIES else s.category or "Process"
    s.org_unit_code = f.get("org_unit_code") or None
    s.critical = f.get("critical") == "on"
    try:
        s.min_uo = max(int(f.get("min_uo") or 0), 0)
        s.valid_months = int(f.get("valid_months")) if f.get("valid_months") else None
    except ValueError:
        pass
    s.course_code = f.get("course_code") or None
    from ..services.common import audit

    audit(db, user, "update", "skill", code, "Skill saved")
    db.commit()
    flash(request, f"{code} saved")
    return redirect("/skills/catalog")
