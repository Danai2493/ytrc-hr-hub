"""Internal job board and move criteria for transfers and promotions."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import ApprovalRequest, Employee, Requisition
from app.models.recruit import InternalInterest
from app.services import mobility as J


def _req(db, ref):
    return db.scalar(select(Requisition).where(Requisition.ref == ref))


def test_board_and_raise_hand(login, db):
    sup = login("sup.pcl")  # works in PRD-PCL: can raise a hand for the dryer job, not for their own unit's seat
    page = sup.get("/jobs")
    assert page.status_code == 200 and "MR-2026" not in page.text  # employees see the job, not the internal ref
    req = _req(db, "MR-2026-000002")
    sup.post(f"/jobs/{req.id}", {"note": "I want to learn the dryer line (SAMPLE)"})
    db.expire_all()
    ii = db.scalar(select(InternalInterest).where(InternalInterest.requisition_id == req.id, InternalInterest.employee_id == "YTR807"))
    assert ii and ii.status == "interested" and ii.checks
    sup.post(f"/jobs/{req.id}", {"note": "again"})  # twice: refused
    db.expire_all()
    assert len(db.scalars(select(InternalInterest).where(InternalInterest.employee_id == "YTR807")).all()) == 1
    # HR sees the hand on the request page; the supervisor's manager page does not show it
    hro = login("hr.officer")
    assert "YTR807" in hro.get(f"/recruit/req/{req.id}").text
    # not_now needs a reason the employee will see
    hro.post(f"/jobs/interest/{ii.id}", {"status": "not_now", "note": ""})
    db.expire_all()
    assert db.get(InternalInterest, ii.id).status == "interested"
    sup.post(f"/jobs/interest/{ii.id}/withdraw")
    db.expire_all()
    assert db.get(InternalInterest, ii.id).status == "withdrawn"
    # someone else cannot withdraw for you
    other = db.scalar(select(InternalInterest).where(InternalInterest.status == "interested", InternalInterest.employee_id != "YTR807"))
    sup.post(f"/jobs/interest/{other.id}/withdraw")
    db.expire_all()
    assert db.get(InternalInterest, other.id).status == "interested"


def test_move_from_board_closes_the_application(login, db):
    hro = login("hr.officer")
    ii = db.scalar(select(InternalInterest).where(InternalInterest.status == "talking"))
    req = ii.requisition
    seats_before = req.open_seats
    r = hro.get(f"/jobs/interest/{ii.id}/move", follow_redirects=False)
    assert r.status_code in (302, 303) and f"/people/{ii.employee_id}/event" in r.headers["location"]
    form = hro.get(r.headers["location"])
    assert form.status_code == 200 and req.position_id in form.text and "interest_id" in form.text
    r = hro.post(f"/people/{ii.employee_id}/event", {"event_type": "transfer", "effective_date": (date.today() + timedelta(days=30)).isoformat(),
                                                     "to_org_unit": req.org_unit_code, "to_position": req.position_id, "to_job": req.job_code,
                                                     "interest_id": str(ii.id), "note": "Internal job (SAMPLE)"})
    assert r.status_code in (302, 303)
    db.expire_all()
    ii = db.get(InternalInterest, ii.id)
    assert ii.status == "moving" and ii.approval_id
    appr = db.get(ApprovalRequest, ii.approval_id)
    assert {c["code"] for c in appr.payload["checks"]} >= {"mv_seat_ok"}
    assert "Move criteria" in hro.get(f"/approvals/{appr.id}").text
    # approve every step
    for _ in range(5):
        db.expire_all()
        appr = db.get(ApprovalRequest, ii.approval_id)
        if appr.status != "pending":
            break
        _approve_step(db, appr)
        db.commit()
    db.expire_all()
    ii = db.get(InternalInterest, ii.id)
    assert ii.status == "moved"
    req = db.get(Requisition, req.id)
    assert req.open_seats == seats_before - 1
    from app.models import EmploymentEvent
    ev = db.scalar(select(EmploymentEvent).where(EmploymentEvent.approval_id == ii.approval_id))
    assert ev and ev.to_position == req.position_id and not ev.applied  # moves on the effective date
    # put the shared demo data back for the other tests
    req.hired -= 1
    req.status, req.closed_on = "open", None
    db.delete(ev)
    db.commit()


def _approve_step(db, appr):
    from app.models import User
    from app.services import approvals
    u = next(u for u in db.scalars(select(User).order_by(User.username)) if approvals.can_decide(u, appr, appr.active_step))
    approvals.decide(db, appr, u, "approved", "ok (SAMPLE)")


def test_promotion_criteria(db):
    emp = db.get(Employee, "YTR806")  # supervisor SV: promotion to MG checks time in grade, grade jump, seat, rating
    out = J.move_checks(db, emp, "POS-0005", None, "MG", "promotion")
    codes = {c["code"] for c in out}
    assert codes & {"mv_seat_full", "mv_seat_ok", "mv_seat_unbudgeted"}
    assert codes & {"mv_rating_ok", "mv_rating_low", "mv_no_rating"}
    assert codes & {"mv_time_in_grade", "mv_time_in_grade_ok"}
    # same grade promotion is flagged
    same = {c["code"] for c in J.move_checks(db, emp, None, None, emp.grade_code, "promotion")}
    assert "mv_promo_same_grade" in same
    # a frozen seat blocks
    from app.models import Position
    pos = db.get(Position, "POS-0017")
    pos.frozen = True
    db.flush()
    assert J.worst(J.move_checks(db, emp, "POS-0017", None, None, "transfer")) == "block"
    db.rollback()
