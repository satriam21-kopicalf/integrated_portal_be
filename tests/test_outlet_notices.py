from datetime import date

from app.alerts import outlet_notices
from app.cotr_ledger import changed


def test_notices_go_to_the_accounts_of_the_place():
    d = date(2026, 11, 24)
    recount = {"id": "c1", "number": "CNT-1", "day": d, "at": "x", "note": "cek susu", "name": "Joglo", "place": "CTG23"}
    fix = {"id": "c2", "number": "CNT-2", "day": d, "at": "2026112410", "name": "Joglo", "place": "CTG23", "items": ["A 1 → 2 GR"] * 6}
    waste = {"id": "w1", "place": "OTHER", "day": d, "item": "Susu", "field": "qty", "old": "1", "new": "2", "reason": "foto", "by": "CC", "on": d}
    out = outlet_notices([recount], [fix], [waste], {"CTG23": ["u1", "u2"]})
    assert [(a["kind"], a["user"]) for a in out] == [("so_recount", "u1"), ("so_recount", "u2"), ("so_fix", "u1"), ("so_fix", "u2")]
    assert "(+2)" in out[2]["body"] and len({a["dedup"] for a in out}) == 4


def test_only_the_changed_menus_are_revised():
    assert changed({"A": 5, "B": 2}, {"A": 5, "C": 1}) == {"B": (2, 0), "C": (0, 1)}
