from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, Grade, Job, JobEvaluation, Position
from ..models.grading import FACTORS, STATUSES
from ..security import check_csrf, current_user, require
from ..services import grading as G
from ..services.common import audit
from .templating import flash, redirect, render

router = APIRouter(prefix="/org/grades")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "auditor")


@router.get("")
def home(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    grades = list(db.scalars(select(Grade).order_by(Grade.rank.desc())))
    jobs = list(db.scalars(select(Job).where(Job.active.is_(True)).order_by(Job.job_family, Job.title_en)))
    evs = {e.job_code: e for e in db.scalars(select(JobEvaluation))}
    people = Counter(e.grade_code for e in db.scalars(select(Employee).where(Employee.status == "Active")))
    jobs_in = Counter(j.default_grade_code for j in jobs)
    return render(request, "grades/home.html", {"grades": grades, "jobs": jobs, "evs": evs, "people": people, "jobs_in": jobs_in,
                                                "found": G.checks(db), "statuses": STATUSES})


@router.post("/ladder", dependencies=[Depends(check_csrf)])
async def save_ladder(request: Request, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    f = await request.form()
    changed = []
    for g in db.scalars(select(Grade)):
        raw = (f.get(f"min_{g.code}") or "").strip()
        band = (f.get(f"band_{g.code}") or "").strip()[:40] or None
        try:
            mp = int(raw) if raw else None
        except ValueError:
            flash(request, "Points must be whole numbers", "bad")
            return redirect("/org/grades")
        if mp != g.min_points or band != g.band_label:
            changed.append(f"{g.code}: {g.min_points}->{mp}")
            g.min_points, g.band_label = mp, band
    for e in db.scalars(select(JobEvaluation)):
        e.suggested_grade = G.suggest(db, e.points)
    audit(db, user, "grade_ladder", "grade", None, "; ".join(changed)[:500] or "no change")
    db.commit()
    flash(request, "Grade ladder saved. Suggestions are updated; agreed grades stay as they are.")
    return redirect("/org/grades")


def _job(db: Session, code: str) -> Job:
    j = db.get(Job, code)
    if j is None:
        raise HTTPException(404)
    return j


@router.get("/job/{code}")
def job(request: Request, code: str, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    j = _job(db, code)
    ev = G.evaluation(db, j)
    return render(request, "grades/job.html", {"j": j, "ev": ev, "factors": FACTORS, "statuses": STATUSES,
                                               "grades": list(db.scalars(select(Grade).order_by(Grade.rank.desc()))),
                                               "positions": list(db.scalars(select(Position).where(Position.job_code == j.code))),
                                               "people": list(db.scalars(select(Employee).where(Employee.status == "Active", Employee.job_code == j.code)))})


@router.post("/job/{code}", dependencies=[Depends(check_csrf)])
async def job_save(request: Request, code: str, db: Session = Depends(get_db), user=Depends(current_user)):
    j = _job(db, code)
    f = await request.form()
    try:
        if f.get("do") == "ai":
            if not user.has(*G.HR):
                raise G.GradingError("Only HR evaluates jobs")
            err = G.ai_suggest(db, j)
            if err:
                raise G.GradingError(err)
            msg = "AI suggestion filled in. Check each level before saving."
        elif f.get("do") == "agree":
            G.agree(db, user, j, f.get("grade") or "", (f.get("reason") or "").strip())
            msg = "Job grade agreed."
        else:
            G.save(db, user, j, f)
            msg = "Saved."
        db.commit()
        flash(request, msg)
    except G.GradingError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/org/grades/job/{code}")
