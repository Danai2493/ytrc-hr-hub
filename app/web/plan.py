"""Plan ahead: succession for critical positions, early leaving-risk flags, workforce plan."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, OrgUnit, Position, StayTalk
from ..models.plan import READINESS, VACANCY_RISK
from ..security import check_csrf, current_user, require
from ..services import plan as P
from .templating import flash, redirect, render

router = APIRouter(prefix="/plan")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "auditor")
CTX = {"readiness": READINESS, "impacts": VACANCY_RISK}


def _units(db: Session) -> dict:
    return {u.code: u for u in db.scalars(select(OrgUnit))}


@router.get("")
def succession(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    rows = []
    for pos in P.critical_positions(db, user):
        plan = P.plan_for(db, pos)
        rows.append((pos, plan, P.position_flags(db, pos, plan)))
    rows.sort(key=lambda r: (-sum(1 for f in r[2] if f["level"] == "block"), -sum(1 for f in r[2] if f["level"] == "flag"), r[0].position_id))
    return render(request, "plan/succession.html", {**CTX, "rows": rows, "bench": P.bench(db, [r[0] for r in rows]), "units": _units(db),
                                                   "tab": "succession", "can_risk": P.can_see_risk(user)})


def _pos(db: Session, user, pid: str) -> Position:
    pos = db.get(Position, pid)
    if pos is None or not pos.critical or not P.can_see_succession(db, user, pos):
        raise HTTPException(404)
    return pos


@router.get("/position/{pid}")
def position(request: Request, pid: str, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    pos = _pos(db, user, pid)
    plan = P.plan_for(db, pos)
    talent = {s.employee_id: P.latest_talent(db, s.employee_id) for s in plan.successors}
    inc = [(e, P.retire_on(db, e), P.latest_talent(db, e.employee_id)) for e in pos.incumbents]
    return render(request, "plan/position.html", {**CTX, "pos": pos, "plan": plan, "flags": P.position_flags(db, pos, plan), "talent": talent,
                                                 "incumbents": inc, "ideas": P.suggestions(db, pos, plan) if P.can_edit_succession(db, user, pos) else [],
                                                 "can_edit": P.can_edit_succession(db, user, pos), "units": _units(db), "tab": "succession",
                                                 "can_risk": P.can_see_risk(user)})


@router.post("/position/{pid}", dependencies=[Depends(check_csrf)])
async def position_save(request: Request, pid: str, db: Session = Depends(get_db), user=Depends(current_user)):
    pos = _pos(db, user, pid)
    f = await request.form()
    try:
        do = f.get("do")
        if do == "add":
            P.add_successor(db, user, pos, f.get("employee_id", ""), f.get("readiness", ""), f.get("gaps", ""))
            msg = "Successor added."
        elif do == "remove":
            P.remove_successor(db, user, pos, int(f.get("sid") or 0))
            msg = "Removed."
        else:
            P.save_plan(db, user, pos, f)
            msg = "Saved."
        db.commit()
        flash(request, msg)
    except (P.PlanError, ValueError) as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(f"/plan/position/{pid}")


@router.get("/risk")
def risk(request: Request, show: str = "watch", unit: str = "", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm"))):
    rows = P.risks(db, user, min_level="all" if show == "all" else "watch")
    units = _units(db)
    if unit:
        rows = [r for r in rows if P.dept_of_unit(db, r.employee.org_unit_code) == unit]
    depts = sorted({P.dept_of_unit(db, e.org_unit_code) for e in db.scalars(select(Employee).where(Employee.status == "Active"))} - {None})
    return render(request, "plan/risk.html", {**CTX, "rows": rows, "show": show, "unit": unit, "units": units, "depts": depts, "tab": "risk",
                                             "can_risk": True, "high": sum(1 for r in rows if r.level == "high"),
                                             "watch": sum(1 for r in rows if r.level == "watch"),
                                             "no_talk": sum(1 for r in rows if r.level == "high" and not r.last_talk),
                                             "signals": P.SIGNALS, "how": P.HOW, "dept_of": lambda c: P.dept_of_unit(db, c)})


@router.get("/risk/{emp_id}")
def risk_person(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm"))):
    e = db.get(Employee, emp_id)
    if e is None or e.employee_id == user.employee_id:
        raise HTTPException(404)
    sig = P.signals_for(db, e, date.today())
    score = sum(x["weight"] for x in sig)
    talks = list(db.scalars(select(StayTalk).where(StayTalk.employee_id == emp_id).order_by(StayTalk.on.desc())))
    return render(request, "plan/person.html", {**CTX, "e": e, "sig": sig, "score": score, "talks": talks, "units": _units(db), "tab": "risk",
                                               "can_risk": True, "level": P.level_of(score),
                                               "today": date.today()})


@router.post("/risk/{emp_id}", dependencies=[Depends(check_csrf)])
async def risk_talk(request: Request, emp_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    f = await request.form()
    try:
        on = date.fromisoformat(f.get("on") or date.today().isoformat())
        fu = date.fromisoformat(f["follow_up_on"]) if f.get("follow_up_on") else None
        P.add_talk(db, user, emp_id, on, f.get("note") or "", fu)
        db.commit()
        flash(request, "Stay talk recorded.")
    except (P.PlanError, ValueError) as ex:
        db.rollback()
        flash(request, str(ex), "bad")
    return redirect(f"/plan/risk/{emp_id}")


@router.get("/workforce")
def workforce(request: Request, months: int = 12, turnover: str = "", growth: str = "0", dept: str = "", db: Session = Depends(get_db),
              user=Depends(require(*VIEW))):
    months = 24 if months == 24 else 12
    try:
        tv = float(turnover) if turnover.strip() else None
        gr = float(growth or 0)
    except ValueError:
        tv, gr = None, 0.0
    f = P.forecast(db, user, months=months, turnover=tv, growth=gr)
    row = f.rows.get(dept) if dept else None
    view = row or f.total
    starts = {d: P.start_hiring(f, d) for d in f.rows}
    return render(request, "plan/workforce.html", {**CTX, "f": f, "months": months, "turnover": turnover, "growth": growth, "dept": dept if row else "",
                                                  "view": view, "starts": starts, "labels": [m.strftime("%m/%y") for m in f.months], "units": _units(db), "tab": "workforce",
                                                  "can_risk": P.can_see_risk(user), "today": date.today()})
