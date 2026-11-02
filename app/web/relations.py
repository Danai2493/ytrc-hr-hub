from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Employee, ErCase, OrgUnit
from ..models.relations import CATEGORIES, KINDS, OUTCOMES, STATUSES
from ..security import check_csrf, current_user, require
from ..services import relations as R
from ..services.common import get_setting
from .templating import flash, redirect, render

router = APIRouter(prefix="/er")
VIEW = ("hr", "hr_manager", "gm", "dept_head", "supervisor")
CTX = {"kinds": KINDS, "cats": CATEGORIES, "outcomes": OUTCOMES, "statuses": STATUSES}


@router.get("")
def home(request: Request, status: str = "", kind: str = "", db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    rows = R.visible(db, user, status, kind)
    every = R.visible(db, user)
    t = date.today()
    late = [c for c in every if c.reply_due and c.status != "closed" and c.reply_due < t]
    in_force = [c for c in every if c.outcome == "written" and c.decided_at and c.warning_until and c.warning_until >= t]
    return render(request, "er/home.html", {**CTX, "rows": rows, "status": status, "kind": kind, "late": late, "in_force": in_force,
                                            "open_n": sum(1 for c in every if c.status != "closed"), "today": t,
                                            "people": {e.employee_id: e for e in db.scalars(select(Employee))}})


@router.post("", dependencies=[Depends(check_csrf)])
async def create(request: Request, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    f = await request.form()
    try:
        c = R.open_case(db, user, dict(f))
        db.commit()
        flash(request, "Case opened.")
        return redirect(f"/er/{c.id}")
    except R.ErError as e:
        db.rollback()
        flash(request, str(e), "bad")
        return redirect("/er#new")


def _case(db: Session, user, cid: int) -> ErCase:
    c = db.get(ErCase, cid)
    if c is None or not R.can_see(db, user, c):
        raise HTTPException(404)
    return c


@router.get("/{cid}")
def detail(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require(*VIEW))):
    c = _case(db, user, cid)
    found = request.session.pop("er_found", None)
    return render(request, "er/case.html", {**CTX, "c": c, "found": found, "can_work": R.can_work(user, c), "can_decide": R.can_decide(user, c),
                                            "deciders": R.decider_roles(c), "other": db.get(Employee, c.other_employee_id) if c.other_employee_id else None,
                                            "unit": db.get(OrgUnit, c.org_unit_code) if c.org_unit_code else None,
                                            "history": [h for h in db.scalars(select(ErCase).where(ErCase.employee_id == c.employee_id, ErCase.id != c.id)
                                                                                   .order_by(ErCase.incident_on.desc())) if R.can_see(db, user, h)],
                                            "in_force": R.warnings_in_force(db, c.employee_id, date.today(), skip=c.id), "today": date.today()})


@router.post("/{cid}", dependencies=[Depends(check_csrf)])
async def act(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    c = _case(db, user, cid)
    f = await request.form()
    do = f.get("do")
    try:
        if do == "work":
            R.save_work(db, user, c, f.get("statement") or "", f.get("statement_on") or "", f.get("findings") or "")
            msg = "Saved."
        elif do == "propose":
            found = R.propose(db, user, c, f)
            request.session["er_found"] = found
            if any(x["level"] == "block" for x in found):
                db.rollback()
                flash(request, "Fix the points in red first.", "bad")
                return redirect(f"/er/{cid}#decide")
            msg = "Outcome proposed. It now needs the decision."
        elif do in ("agree", "send_back"):
            R.decide(db, user, c, do == "agree", (f.get("note") or "").strip())
            msg = "Decision recorded." if do == "agree" else "Sent back."
        elif do == "close":
            R.close(db, user, c, f.get("letter_on") or "", f.get("signed") or "")
            msg = "Case closed."
        else:
            raise R.ErError("Unknown action")
        db.commit()
        flash(request, msg)
    except (R.ErError, ValueError) as e:
        db.rollback()
        flash(request, str(e) or "Check the dates", "bad")
    return redirect(f"/er/{cid}")


@router.get("/{cid}/letter")
def letter(request: Request, cid: int, db: Session = Depends(get_db), user=Depends(require("hr", "hr_manager", "gm"))):
    c = _case(db, user, cid)
    if c.kind != "discipline" or c.outcome not in ("written", "suspension", "dismissal") or not c.decided_at:
        raise HTTPException(404)
    company = get_setting(db, "company_name_th") or "YTRC (ชื่อบริษัทตั้งใน Admin: company_name_th)"
    return render(request, "er/letter.html", {**CTX, "c": c, "company": company, "unit": db.get(OrgUnit, c.org_unit_code) if c.org_unit_code else None})
