from __future__ import annotations

import math
from datetime import date, timedelta
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Assessment, AssessmentRound, Competency, Course, DevelopmentPlan, Employee, GrowthPath
from ..models.competency import ACTION_TYPES, ANSWER_SCALE, AUDIENCES
from ..security import check_csrf, current_user, require
from ..services import growth as G
from ..services.common import user_scope
from ..seed.competency import CLUSTERS
from ..i18n import gettext
from .templating import flash, lang_of, redirect, render

router = APIRouter(prefix="/growth")
HR = ("hr", "hr_manager")


def radar(labels: list[str], series: list[tuple[str, str, list[float | None]]], size: int = 320) -> Markup:
    """Spider chart drawn on the server, scale 1-5. series: (name, colour, values)."""
    n = len(labels)
    if n < 3:
        return Markup("")
    cx = cy = size / 2
    r = size / 2 - 58

    def pt(i: int, v: float) -> tuple[float, float]:
        a = -math.pi / 2 + 2 * math.pi * i / n
        k = r * (v - 0) / 5
        return cx + k * math.cos(a), cy + k * math.sin(a)

    out = [f'<svg viewBox="-80 0 {size + 160} {size}" role="img" class="radar">']
    for ring in (1, 2, 3, 4, 5):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, ring) for i in range(n)))
        out.append(f'<polygon points="{pts}" fill="none" stroke="var(--line)" stroke-width="1"/>')
    for i, lab in enumerate(labels):
        x, y = pt(i, 5)
        lx, ly = pt(i, 6.1)
        anchor = "middle" if abs(lx - cx) < 8 else ("start" if lx > cx else "end")
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="var(--line)"/>')
        out.append(f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="{anchor}" font-size="11">{escape(lab[:26])}</text>')
    for name, color, vals in series:
        if not any(v for v in vals):
            continue
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, v or 0) for i, v in enumerate(vals)))
        dash = ' stroke-dasharray="5 4"' if name.startswith("~") else ""
        fill = "none" if dash else color
        out.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity=".18" stroke="{color}" stroke-width="2"{dash}><title>{escape(name.lstrip("~"))}</title></polygon>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:{c}"></i>{escape(nm.lstrip("~"))}</span>' for nm, c, v in series if any(x for x in v))
    return Markup("".join(out) + f'<div class="legend">{legend}</div>')


def _name(c, lang: str) -> str:
    return (c.name_th if lang == "th" and getattr(c, "name_th", None) else c.name_en)


def _assessment(db: Session, aid: int, user) -> Assessment:
    a = db.get(Assessment, aid)
    if a is None or not G.can_see(db, user, a.employee):
        raise HTTPException(404)
    return a


@router.get("")
def home(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    mine = list(db.scalars(select(Assessment).where(Assessment.employee_id == user.employee_id).order_by(Assessment.id.desc()))) if user.employee_id else []
    plans = list(db.scalars(select(DevelopmentPlan).where(DevelopmentPlan.employee_id == user.employee_id).order_by(DevelopmentPlan.id.desc()))) if user.employee_id else []
    rounds = list(db.scalars(select(AssessmentRound).order_by(AssessmentRound.opens_on.desc())))
    team = []
    if user.has("supervisor", "dept_head", "hr", "hr_manager", "gm"):
        scope = user_scope(db, user)
        cur = next((r for r in rounds if r.status == "open"), rounds[0] if rounds else None)
        if cur:
            for a in db.scalars(select(Assessment).where(Assessment.round_id == cur.id)):
                if a.employee_id != user.employee_id and (scope.all or a.employee.org_unit_code in scope.org_units):
                    plan = db.scalar(select(DevelopmentPlan).where(DevelopmentPlan.assessment_id == a.id))
                    team.append((a, plan))
        team.sort(key=lambda x: (x[0].employee.org_unit_code, x[0].employee_id))
    return render(request, "growth/home.html", {"mine": mine, "plans": plans, "rounds": rounds, "team": team, "is_hr": user.has(*HR),
                                                "groups": ["Operator", "Staff", "Manager", "Japanese Management"], "default_close": date.today() + timedelta(days=21),
                                                "audiences": AUDIENCES})


@router.post("/rounds", dependencies=[Depends(check_csrf)])
async def new_round(request: Request, db: Session = Depends(get_db), user=Depends(require(*HR))):
    f = await request.form()
    try:
        rnd = G.open_round(db, user, f.get("name") or f"{date.today().year} round", date.fromisoformat(f.get("opens_on")),
                           date.fromisoformat(f.get("closes_on")), f.getlist("groups"), f.get("note") or "")
        db.commit()
        flash(request, f"{rnd.name} opened")
        return redirect(f"/growth/rounds/{rnd.id}")
    except (G.GrowthError, ValueError, TypeError) as e:
        db.rollback()
        flash(request, str(e) or "Check the dates", "bad")
        return redirect("/growth")


@router.get("/rounds/{rid}")
def round_page(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm", "dept_head", "supervisor"))):
    rnd = db.get(AssessmentRound, rid)
    if rnd is None:
        raise HTTPException(404)
    scope = user_scope(db, user)
    ids = None if scope.all else {e for e, u in db.execute(select(Employee.employee_id, Employee.org_unit_code)) if u in scope.org_units}
    s = G.round_summary(db, rnd, ids)
    comps = G.competencies(db)
    lang = lang_of(request)
    from ..models import OrgUnit

    unit_names = {u.code: u.name_en for u in db.scalars(select(OrgUnit))}
    labels = [c.code for c in comps.values() if c.code in s["company"]]
    chart = radar([_name(comps[c], lang) for c in labels], [(gettext(lang, "Company average"), "var(--s1)", [s["company"][c] for c in labels])], 420) if labels else ""
    return render(request, "growth/round.html", {"rnd": rnd, "s": s, "clusters": CLUSTERS, "unit_names": unit_names, "chart": chart,
                                                 "is_hr": user.has(*HR), "min_group": G.MIN_GROUP})


@router.post("/rounds/{rid}/close", dependencies=[Depends(check_csrf)])
def close_round(request: Request, rid: int, db: Session = Depends(get_db), user=Depends(require(*HR))):
    rnd = db.get(AssessmentRound, rid)
    if rnd is None:
        raise HTTPException(404)
    G.close_round(db, user, rnd)
    db.commit()
    return redirect(f"/growth/rounds/{rid}")


@router.get("/a/{aid}")
def fill(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _assessment(db, aid, user)
    own = user.employee_id == a.employee_id
    if not own and not user.has(*HR):
        return redirect(f"/growth/a/{aid}/report")
    if a.status == "submitted":
        return redirect(f"/growth/a/{aid}/report")
    qs = G.questions_for(db, a.audience)
    comps = G.competencies(db)
    return render(request, "growth/fill.html", {"a": a, "qs": qs, "comps": comps, "scale": ANSWER_SCALE, "clusters": CLUSTERS,
                                                "answers": a.answers or {}, "own": own, "audiences": AUDIENCES})


@router.post("/a/{aid}", dependencies=[Depends(check_csrf)])
async def fill_save(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _assessment(db, aid, user)
    own = user.employee_id == a.employee_id
    if not own and not user.has(*HR):
        raise HTTPException(403)
    f = await request.form()
    raw = {k[2:]: v for k, v in f.items() if k.startswith("q_")}
    try:
        G.save_answers(db, a, user, raw, f.get("submit") == "1", "web" if own else "paper")
        db.commit()
    except G.GrowthError as e:
        db.commit()  # keep the answers given so far
        flash(request, str(e), "bad")
        return redirect(f"/growth/a/{aid}")
    if a.status == "submitted":
        flash(request, "Thank you. Your report is ready.")
        return redirect(f"/growth/a/{aid}/report")
    flash(request, "Saved. You can finish later.")
    return redirect(f"/growth/a/{aid}")


@router.get("/a/{aid}/report")
def report_page(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _assessment(db, aid, user)
    if a.status != "submitted":
        flash(request, "Not answered yet", "warn")
        return redirect("/growth")
    rep = G.report(db, a)
    lang = lang_of(request)
    comps = rep.comps
    codes = [c for c in comps if c in rep.scores]
    t = lambda x: ("~" if x.startswith("~") else "") + gettext(lang, x.lstrip("~"))
    series = [(t("Self view"), "var(--s1)", [rep.scores.get(c) for c in codes])]
    if a.manager_scores:
        series.append((t("Supervisor view"), "var(--s2)", [a.manager_scores.get(c) for c in codes]))
    if rep.previous:
        series.append((t("~Last round"), "var(--ink-3)", [(rep.previous.scores or {}).get(c) for c in codes]))
    main = radar([_name(comps[c], lang) for c in codes], series, 460)
    path_charts = []
    for pf in rep.paths[:3]:
        pc = [c for c in pf.path.targets if c in rep.scores]
        path_charts.append((pf, radar([_name(comps[c], lang) for c in pc], [(t("Self view"), "var(--s1)", [rep.scores[c] for c in pc]),
                                                                               (t("~What helps on this path"), "var(--s4)", [pf.path.targets[c] for c in pc])], 300)))
    plan = db.scalar(select(DevelopmentPlan).where(DevelopmentPlan.assessment_id == a.id))
    return render(request, "growth/report.html", {"a": a, "rep": rep, "main": main, "path_charts": path_charts, "clusters": CLUSTERS,
                                                  "is_sup": G.is_supervisor_of(db, user, a.employee), "own": user.employee_id == a.employee_id,
                                                  "plan": plan, "nm": lambda c: _name(comps[c], lang) if c in comps else c, "lang": lang})


@router.post("/a/{aid}/manager", dependencies=[Depends(check_csrf)])
async def manager(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _assessment(db, aid, user)
    if not G.is_supervisor_of(db, user, a.employee):
        raise HTTPException(403)
    f = await request.form()
    G.manager_view(db, a, user, {k[2:]: v for k, v in f.items() if k.startswith("m_")}, f.get("note") or "")
    db.commit()
    flash(request, "Supervisor view saved")
    return redirect(f"/growth/a/{aid}/report")


@router.post("/a/{aid}/plan", dependencies=[Depends(check_csrf)])
def start_plan(request: Request, aid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    a = _assessment(db, aid, user)
    plan = db.scalar(select(DevelopmentPlan).where(DevelopmentPlan.assessment_id == a.id))
    if plan is None:
        plan = G.create_plan(db, user, a.employee, a)
        db.commit()
    return redirect(f"/growth/plan/{plan.id}")


def _plan(db: Session, pid: int, user) -> DevelopmentPlan:
    p = db.get(DevelopmentPlan, pid)
    if p is None or not G.can_see(db, user, p.employee):
        raise HTTPException(404)
    return p


@router.get("/plan/{pid}")
def plan_page(request: Request, pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = _plan(db, pid, user)
    a = db.get(Assessment, p.assessment_id) if p.assessment_id else None
    rep = G.report(db, a) if a and a.scores else None
    lang = lang_of(request)
    comps = G.competencies(db)
    return render(request, "growth/plan.html", {
        "p": p, "rep": rep, "comps": comps, "paths": list(db.scalars(select(GrowthPath).where(GrowthPath.active.is_(True)))),
        "courses": list(db.scalars(select(Course).where(Course.active.is_(True)).order_by(Course.code))), "types": ACTION_TYPES,
        "can_edit": p.status != "done" and (user.employee_id == p.employee_id or G.is_supervisor_of(db, user, p.employee) or user.has(*HR)),
        "can_agree": p.status == "draft" and (G.is_supervisor_of(db, user, p.employee) or user.has(*HR)) and user.employee_id != p.employee_id,
        "nm": lambda c: _name(comps[c], lang) if c in comps else c, "lang": lang,
    })


@router.post("/plan/{pid}", dependencies=[Depends(check_csrf)])
async def plan_save(request: Request, pid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    p = _plan(db, pid, user)
    if not (user.employee_id == p.employee_id or G.is_supervisor_of(db, user, p.employee) or user.has(*HR)):
        raise HTTPException(403)
    f = await request.form()
    action = f.get("do") or "save"
    if action == "suggest":
        G.save_plan(db, user, p, f)
        err = G.suggest(db, p, [f.get(f"c{i}") for i in range(3) if f.get(f"c{i}")])
        db.commit()
        flash(request, err or "AI suggested actions. Change anything that does not fit, then save.", "bad" if err else "warn")
    elif action == "agree":
        G.save_plan(db, user, p, f)
        try:
            n = G.agree(db, user, p)
            db.commit()
            flash(request, f"Plan agreed. {n} course(s) added to training needs." if n else "Plan agreed.")
        except G.GrowthError as e:
            db.rollback()
            flash(request, str(e), "bad")
    elif action == "done":
        G.save_plan(db, user, p, f)
        p.status = "done"
        db.commit()
        flash(request, "Plan marked as done")
    else:
        G.save_plan(db, user, p, f)
        db.commit()
        flash(request, "Plan saved")
    return redirect(f"/growth/plan/{pid}")


@router.get("/framework")
def framework(request: Request, print: int = 0, audience: str = "core", db: Session = Depends(get_db), user=Depends(current_user)):
    comps = G.competencies(db)
    from ..models import CompetencyQuestion

    qs = list(db.scalars(select(CompetencyQuestion).where(CompetencyQuestion.active.is_(True)).order_by(CompetencyQuestion.sort_order)))
    if print:
        aud = audience if audience in AUDIENCES else "core"
        return render(request, "growth/print.html", {"qs": G.questions_for(db, aud), "comps": comps, "scale": ANSWER_SCALE, "audience": aud, "audiences": AUDIENCES})
    return render(request, "growth/framework.html", {"comps": comps, "qs": qs, "clusters": CLUSTERS, "audiences": AUDIENCES,
                                                     "paths": list(db.scalars(select(GrowthPath).where(GrowthPath.active.is_(True)))), "lang": lang_of(request)})
