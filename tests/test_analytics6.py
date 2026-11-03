"""Phase 6 analytics: retention, absence patterns, people cost (totals only), workforce mix, cost per hire."""
from datetime import date, timedelta

from sqlalchemy import inspect, select

from app.models import PayTotal, User
from app.services import analytics as A
from app.services.common import user_scope


def test_pages_and_access(login):
    hrm, head = login("hr.manager"), login("prod.head")
    for url in ("/dash/retention", "/dash/mix", "/dash/cost", "/dash/attendance", "/recruit"):
        assert hrm.get(url).status_code == 200, url
    assert head.get("/dash/retention").status_code == 200
    assert head.get("/dash/cost").status_code == 403  # people cost: HR, HR Manager, GM only
    assert "Cost per hire" in hrm.get("/recruit").text


def test_numbers_add_up(db):
    u = db.scalar(select(User).where(User.username == "hr.manager"))
    sc = user_scope(db, u)
    r = A.retention(db, sc)
    assert r["leavers"] == r["voluntary"] + r["involuntary"]
    assert sum(n for _, n in r["tenure"]) == r["leavers"]
    m = A.workforce_mix(db, sc)
    assert sum(m["grid"].values()) == m["total"] == sum(n for _, n in m["tenure"])
    c = A.people_cost(db, sc)
    assert c["has"] and len(c["labels"]) == 12 and all(b >= 0 for b in c["base"])
    assert not {col.key for col in inspect(PayTotal).columns} & {"employee_id", "salary"}  # totals per unit only
    p = A.absence_patterns(db, sc, date.today() - timedelta(days=90), date.today())
    for e, spells, days, score in p["bradford"]:
        assert 1 <= spells <= days and score == spells * spells * days
