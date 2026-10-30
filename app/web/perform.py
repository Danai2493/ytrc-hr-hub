from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Appraisal, DevelopmentPlan, Employee, Goal, KpiDefinition, OrgUnit, PerformanceCycle
from ..models.perform import APPRAISAL_STATUSES, DIRECTIONS, FORMS, OPERATOR_PARTS, RATINGS
from ..security import check_csrf, current_user, require
from ..services import growth as G
from ..services import perform as P
from ..services.common import user_scope
from .templating import flash, lang_of, redirect, render

router = APIRouter(prefix="/perform")
HR = ("hr", "hr_manager")
CTX = {"ratings": RATINGS, "statuses": APPRAISAL_STATUSES, "directions": DIRECTIONS, "forms": FORMS, "parts": OPERATOR_PARTS, "letter": P.letter}


def _cycle(db: Session, cid: int) -> PerformanceCycle:
    c = db.get(PerformanceCycle, cid)
    if c is None:
        raise HTTPException(404)
    return c


def _current(db: Session) -> PerformanceCycle | None:
    return db.scalar(select(PerformanceCycle).order_by(PerformanceCycle.period_start.desc(), PerformanceCycle.id.desc()))


def _visible(db: Session, user, c: PerformanceCycle) -> list[Appraisal]:
    rows = list(db.scalars(select(Appraisal).where(Appraisal.cycle_id == c.id).order_by(Appraisal.org_unit_code, Appraisal.employee_id)))
    if user.has("hr", "hr_manager", "gm", "auditor"):
        return rows
    scope = user_scope(db, user)
    me = user.employee_id
    return [a for a in rows if a.employee_id != me and (me in (a.reviewer_employee_id, a.endorser_employee_id) or a.org_unit_code in scope.org_units)]


@router.get("")
def home(request: Request, cycle: int = 0, db: Session = Depends(get_db), user=Depends(current_user)):
    cycles = list(db.scalars(select(PerformanceCycle).order_by(PerformanceCycle.period_start.desc())))
    c = db.get(PerformanceCycle, cycle) if cycle else (cycles[0] if cycles else None)
    mine = db.scalar(select(Appraisal).where(Appraisal.cycle_id == c.id, Appraisal.employee_id == user.employee_id)) if c and user.employee_id else None
    team = _visible(db, user, c) if c else []
    st = P.stats(db, c, team) if c else None
    todo = [a for a in team if (a.reviewer_employee_id == user.employee_id and a.status in ("goals", "goals_agreed", "self", "manager"))
            or (a.endorser_employee_id == user.employee_id and a.status == "endorse")]
    t = date.today()
    return render(request, "perform/home.html", {**CTX, "cycles": cycles, "c": c, "mine": mine, "team": team, "st": st, "todo": todo,
                                                 "is_hr": user.has(*HR), "groups": ["Operator", "Staff", "Manager", "Japanese Management"],
                                                 "defaults": {"name": f"{t.year + 1} performance year", "start": date(t.year + 1, 1, 1), "end": date(t.year + 1, 12, 31),
                                                              "goals_due": date(t.year + 1, 2, 15), "midyear": date(t.year + 1, 7, 15), "review_due": date(t.year + 2, 1, 20)},
                                                 "units": {u.code: u for u in db.scalars(select(OrgUnit))},
                                                 "people": {e.employee_id: e for e in db.scalars(select(Employee).where(Employee.employee_group != "Operator"))}})


@router.post("/cycles", dependencies=[Depends(check_csrf)])
async def new_cycle(request: Request, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    f = await request.form()
    try:
        c, skipped = P.open_cycle(db, user, f.get("name") or "Performance year", date.fromisoformat(f.get("start")), date.fromisoformat(f.get("end")),
                                  date.fromisoformat(f.get("goals_due")), date.fromisoformat(f.get("midyear")) if f.get("midyear") else None,
                                  date.fromisoformat(f.get("review_due")), f.getlist("groups"))
        db.commit()
        flash(request, f"{c.name} opened." + (f" {len(skipped)} people joined less than 3 months before the year end and are not rated: probation covers them." if skipped else ""))
        return redirect(f"/perform?cycle={c.id}")
    except (P.PerformError, ValueError, TypeError) as e:
        db.rollback()
        flash(request, str(e) or "Check the dates", "bad")
        return redirect("/perform")


@router.post("/cycles/{cid}/status", dependencies=[Depends(check_csrf)])
def cycle_status(request: Request, cid: int, status: str = Form(...), db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    c = _cycle(db, cid)
    try:
        if status == "closed":
            n = P.release(db, user, c)
            flash(request, f"{n} results released. Employees can now read and acknowledge them.")
        else:
            P.advance_cycle(db, user, c, status)
        db.commit()
    except P.PerformError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/perform?cycle={cid}")


# ---------- goals ----------

@router.get("/cycles/{cid}/goals")
def goals_page(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    c = _cycle(db, cid)
    goals = list(db.scalars(select(Goal).where(Goal.cycle_id == c.id).order_by(Goal.level, Goal.org_unit_code, Goal.id)))
    scope = user_scope(db, user)
    units = [u for u in db.scalars(select(OrgUnit).where(OrgUnit.active.is_(True)).order_by(OrgUnit.sort_order, OrgUnit.code))]
    can_units = [u for u in units if scope.all or u.code in scope.org_units] if user.has("hr_manager", "gm", "dept_head", "supervisor", "hr") else []
    return render(request, "perform/goals.html", {**CTX, "c": c, "goals": goals, "score": {g.id: P.goal_score(g) for g in goals},
                                                  "kpis": list(db.scalars(select(KpiDefinition).where(KpiDefinition.active.is_(True)).order_by(KpiDefinition.code))),
                                                  "units": {u.code: u for u in units}, "can_units": can_units, "company_ok": user.has("hr_manager", "gm"),
                                                  "editable": {g.id: (g.level == "company" and user.has("hr_manager", "gm")) or (g.level == "unit" and (scope.all or g.org_unit_code in scope.org_units))
                                                               for g in goals}})


@router.post("/cycles/{cid}/goals", dependencies=[Depends(check_csrf)])
async def goals_save(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    c = _cycle(db, cid)
    f = await request.form()
    g = db.get(Goal, int(f.get("goal_id"))) if f.get("goal_id") else None
    if g is not None and g.cycle_id != c.id:
        raise HTTPException(404)
    try:
        data = dict(f)
        if g is not None:
            data.setdefault("level", g.level)
            data.setdefault("org_unit_code", g.org_unit_code)
        P.save_goal(db, user, c, data, g)
        db.commit()
        flash(request, "Goal saved")
    except P.PerformError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/perform/cycles/{cid}/goals")


@router.get("/kpis")
def kpis(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    from ..models.perform import DATA_SOURCES

    return render(request, "perform/kpis.html", {**CTX, "rows": list(db.scalars(select(KpiDefinition).order_by(KpiDefinition.perspective, KpiDefinition.code))),
                                                 "sources": DATA_SOURCES})


# ---------- one appraisal ----------

def _a(db: Session, aid: int, user) -> Appraisal:
    a = db.get(Appraisal, aid)
    if a is None or not P.can_view(db, user, a):
        raise HTTPException(404)
    return a


@router.get("/a/{aid}")
def appraisal(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _a(db, aid, user)
    own = user.employee_id == a.employee_id
    if a.status not in ("released",) and a.cycle.status in ("review", "calibration") and (P.is_reviewer(user, a) or user.has(*HR)):
        P.collect_facts(db, a)
        db.commit()
    goals = P.unit_goals(db, a.cycle, a.org_unit_code)
    comps = G.competencies(db)
    lang = lang_of(request)
    plan = db.get(DevelopmentPlan, a.plan_id) if a.plan_id else None
    show_rating = not own or a.status == "released"
    return render(request, "perform/appraisal.html", {
        **CTX, "a": a, "c": a.cycle, "own": own, "is_rev": P.is_reviewer(user, a), "is_end": user.employee_id == a.endorser_employee_id,
        "is_hr": user.has(*HR), "can_goals": P.can_set_goals(user, a), "checks": P.item_checks(a.items or []) if a.form == "staff" else [],
        "goals": goals, "goal_score": {g.id: P.goal_score(g) for g in goals}, "comps": comps, "kpis": list(db.scalars(select(KpiDefinition).where(KpiDefinition.active.is_(True)).order_by(KpiDefinition.code))),
        "nm": lambda code: (comps[code].name_th if lang == "th" and comps[code].name_th else comps[code].name_en) if code in comps else code,
        "suggested": P.suggest_parts(db, a) if a.form == "operator" else {}, "plan": plan, "show_rating": show_rating,
        "people": {e.employee_id: e for e in db.scalars(select(Employee).where(Employee.employee_id.in_([x for x in (a.reviewer_employee_id, a.endorser_employee_id) if x])))},
        "auto": {i["id"]: P.score_from(P.achievement(i.get("direction") or "up", i.get("target"), i.get("actual"))) for i in a.items or []},
    })


@router.post("/a/{aid}", dependencies=[Depends(check_csrf)])
async def appraisal_post(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _a(db, aid, user)
    f = await request.form()
    do = f.get("do") or ""
    msg = "Saved"
    try:
        if do in ("goals", "agree"):
            P.save_goals(db, user, a, f, agree=do == "agree")
            msg = "Goals agreed" if do == "agree" else "Goals saved"
        elif do in ("ai_suggest", "ai_check"):
            if not P.can_set_goals(user, a):
                raise P.PerformError("Goals cannot be changed now")
            if do == "ai_suggest" and a.items:
                P.save_goals(db, user, a, f)
            err = P.ai_kpis(db, a, "suggest" if do == "ai_suggest" else "check")
            if err:
                raise P.PerformError(err)
            msg = "AI suggested KPIs. Change anything that does not fit, then save." if do == "ai_suggest" else "AI checked the KPIs: see the notes under the table."
        elif do == "midyear":
            P.midyear(db, user, a, f.get("midyear_note") or "", {k[4:]: v for k, v in f.items() if k.startswith("mid_")})
            msg = "Mid-year check-in saved"
        elif do in ("self_save", "self_send"):
            P.self_review(db, user, a, f, do == "self_send")
            msg = "Self review sent to your supervisor" if do == "self_send" else "Self review saved"
        elif do == "skip_self":
            P.skip_self(db, user, a, f.get("reason") or "Self review done in the meeting")
        elif do in ("mgr_save", "mgr_send"):
            P.manager_review(db, user, a, f, do == "mgr_send")
            msg = "Review sent" if do == "mgr_send" else "Review saved"
        elif do == "endorse":
            P.endorse(db, user, a, f.get("endorse_note") or "", int(f.get("rating")) if f.get("rating") else None)
            msg = "Endorsed"
        elif do == "calibrate":
            P.calibrate(db, user, a, int(f.get("rating") or 0), f.get("note") or "")
            db.commit()
            return redirect(f"/perform/cycles/{a.cycle_id}/calibration?unit={a.org_unit_code}")
        elif do == "ack":
            P.acknowledge(db, user, a, f.get("agree") == "1", f.get("ack_note") or "")
            msg = "Thank you. Your answer is recorded."
        else:
            raise HTTPException(400)
        db.commit()
        flash(request, msg, "warn" if do.startswith("ai_") else "ok")
    except P.PerformError as e:
        db.rollback()
        flash(request, str(e), "bad")
    return redirect(f"/perform/a/{aid}")


# ---------- calibration ----------

@router.get("/cycles/{cid}/calibration")
def calibration(request: Request, cid: int, unit: str = "", db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm", "dept_head"))):
    c = _cycle(db, cid)
    rows = _visible(db, user, c)
    units = sorted({a.org_unit_code for a in rows})
    shown = [a for a in rows if not unit or a.org_unit_code == unit]
    st = P.stats(db, c, shown)
    return render(request, "perform/calibration.html", {**CTX, "c": c, "rows": sorted(shown, key=lambda a: -(a.overall or 0)), "units": units, "unit": unit, "st": st,
                                                        "gaps": P.guide_gaps(c, st.ratings), "can_cal": user.has("hr_manager", "gm") and c.status == "calibration",
                                                        "unit_names": {u.code: u.name_en for u in db.scalars(select(OrgUnit))}})


@router.get("/cycles/{cid}/export.csv")
def export(cid: int, db: Session = Depends(get_db), user=Depends(require("hr_manager"))):
    c = _cycle(db, cid)
    if c.status != "closed":
        raise HTTPException(400, "Release the results first")
    buf = io.StringIO()
    csv.writer(buf).writerows(P.export_rows(db, c))
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="ratings-{c.period_end.year}.csv"', "Cache-Control": "private, no-store"})
