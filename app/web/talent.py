from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Appraisal, DevelopmentPlan, Employee, OrgUnit, TalentReview
from ..models.perform import RATINGS
from ..models.talent import BOXES, LEVELS, NEXT_MOVES, POTENTIAL_QUESTIONS, RISKS, STATUSES
from ..security import check_csrf, current_user, require
from ..services import talent as T
from ..services.perform import letter
from .templating import flash, redirect, render

router = APIRouter(prefix="/talent")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "supervisor", "auditor")
BOX = {b[0]: {"perf": p, "pot": q, "name": b[1], "action": b[2]} for (p, q), b in BOXES.items()}
CTX = {"boxes": BOX, "levels": LEVELS, "questions": POTENTIAL_QUESTIONS, "risks": RISKS, "moves": NEXT_MOVES, "statuses": STATUSES,
       "ratings": RATINGS, "letter": letter}


def _year(db: Session, year: int) -> int | None:
    ys = T.years(db)
    return year if year in ys else (ys[0] if ys else None)


@router.get("")
def home(request: Request, year: int = 0, unit: str = "", group: str = "", box: int = 0, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    y = _year(db, year)
    rows = T.people(db, user, y) if y else []
    units = {u.code: u for u in db.scalars(select(OrgUnit))}
    if unit:
        rows = [r for r in rows if r[0].org_unit_code == unit]
    if group:
        rows = [r for r in rows if r[0].employee_group == group]
    g = T.grid(rows)
    shown = [r for r in rows if not box or (r[2] and r[2].box == box)]
    done = sum(1 for r in rows if r[2] and r[2].status in ("proposed", "agreed"))
    agreed = sum(1 for r in rows if r[2] and r[2].status == "agreed")
    risky = [r for r in rows if r[2] and r[2].risk == "high" and (r[2].box or 0) >= 6]
    return render(request, "talent/home.html", {**CTX, "years": T.years(db), "y": y, "rows": rows, "shown": shown, "grid": g, "units": units,
                                                "unit": unit, "group": group, "box": box, "done": done, "agreed": agreed, "risky": risky,
                                                "unit_codes": sorted({r[0].org_unit_code for r in T.people(db, user, y)} if y else set())})


def _emp(db: Session, user, emp_id: str) -> Employee:
    e = db.get(Employee, emp_id)
    if e is None or not T.can_view(db, user, e):
        raise HTTPException(404)
    return e


@router.get("/{year}/{emp_id}")
def review(request: Request, year: int, emp_id: str, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    e = _emp(db, user, emp_id)
    t = T.get_or_new(db, year, e)
    history = list(db.scalars(select(Appraisal).where(Appraisal.employee_id == e.employee_id, Appraisal.status == "released").order_by(Appraisal.id.desc()).limit(3)))
    plan = db.scalar(select(DevelopmentPlan).where(DevelopmentPlan.employee_id == e.employee_id).order_by(DevelopmentPlan.id.desc()))
    past = list(db.scalars(select(TalentReview).where(TalentReview.employee_id == e.employee_id, TalentReview.year < year).order_by(TalentReview.year.desc())))
    return render(request, "talent/review.html", {**CTX, "e": e, "t": t, "year": year, "history": history, "plan": plan, "past": past,
                                                  "can_edit": T.can_propose(db, user, e) and (t.status != "agreed" or T.can_agree(user, e)),
                                                  "can_agree": T.can_agree(user, e) and t.status == "proposed",
                                                  "unit": db.get(OrgUnit, e.org_unit_code) if e.org_unit_code else None})


@router.post("/{year}/{emp_id}", dependencies=[Depends(check_csrf)])
async def save(request: Request, year: int, emp_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    e = _emp(db, user, emp_id)
    f = await request.form()
    t = T.get_or_new(db, year, e)
    try:
        if f.get("do") == "agree":
            pot = T._lvl(f.get("potential")) if f.get("potential") else None
            T.agree(db, user, t, e, (f.get("note") or "").strip(), pot)
            msg = "Agreed in the talent review."
        else:
            T.save(db, user, t, e, f, submit=f.get("do") == "propose")
            msg = "Proposed for the talent review meeting." if f.get("do") == "propose" else "Saved."
        db.commit()
        flash(request, msg)
    except T.TalentError as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(f"/talent/{year}/{emp_id}")
