"""Branch groups and Monitoring → Overview (app/branch_groups.py, app/monitoring_overview.py)."""
from datetime import date

import pytest

from app import branch_groups as bg
from app import monitoring_overview as mo
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

OUTLETS = [
    {"code": "CCI01", "name": "Kopi Calf Kemang", "group_id": "g1", "group_code": "CCI"},
    {"code": "CTG54", "name": "Kopi Calf Cempaka Putih", "group_id": "g2", "group_code": "CTG"},
    {"code": "CTG55", "name": "Kopi Calf To Go Tebet", "group_id": "g2", "group_code": "CTG"},
    {"code": "XYZ01", "name": "Kopi Calf Baru", "group_id": None, "group_code": None},
]


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(bg, "outlets", lambda: OUTLETS)
    monkeypatch.setattr(bg, "list_groups", lambda: {"groups": [{"id": "g1", "code": "CCI", "name": "CCI", "branches": [1]},
                                                              {"id": "g2", "code": "CTG", "name": "CTG", "branches": [1, 2]}], "unassigned": []})
    monkeypatch.setattr(bg, "codes_of", lambda g: [o["code"] for o in OUTLETS if o["group_code"] == g or o["group_id"] == g])
    monkeypatch.setattr(mo.pic_checkin, "monitor", lambda a, b, branches, limit=0: {"data": [
        {"branchCode": "CTG54", "userId": "u1", "name": "Andi", "outsideRadius": False, "visitedAt": "2026-10-20T02:00:00+00:00"},
        {"branchCode": "CTG54", "userId": "u2", "name": "Budi", "outsideRadius": True, "visitedAt": "2026-10-21T02:00:00+00:00"}]})
    monkeypatch.setattr(mo.wwwr, "comparison", lambda a, b, branches: {
        "audits": [{"branchCode": "CTG54", "score": 88.0}], "summary": {"conformity": 75.0, "overrated": 3},
        "pics": [{"id": "u1", "audits": 1, "conformity": 75.0, "overrated": 3}]})
    monkeypatch.setattr(mo.wwwr, "actions_of", lambda branches: [{"branch_code": "CTG54", "n": 2}])
    monkeypatch.setattr(mo, "_assigned", lambda ids: {"u1": ["CTG54"]})
    monkeypatch.setattr(mo.pic_checkin, "daily_limits", lambda codes: {c: 1 for c in codes})

    def monitor(form, a, b, branches, limit=0):
        data = [{"branchCode": "CTG54", "userId": "u1", "name": "Andi", "score": 90.0 if form == "product" else 80.0,
                 "counts": {"under": 2 if form == "outlet" else 0}, "submittedAt": "2026-10-20T03:00:00+00:00"}]
        if form == "product":
            data.append({"branchCode": "CCI01", "userId": "u3", "name": "Citra", "score": 100.0, "counts": {"under": 0}, "submittedAt": "2026-10-21T03:00:00+00:00"})
        return {"data": [d for d in data if d["branchCode"] in branches], "sections": [], "findings": []}
    monkeypatch.setattr(mo.vf, "monitor", monitor)


def test_overview_per_group_and_outlet(sources):
    o = mo.overview(date(2026, 10, 1), date(2026, 10, 21), None, None)
    s = o["summary"]
    assert (s["outlets"], s["visited"], s["checkins"], s["outside"], s["productVisits"], s["outletVisits"], s["under"], s["pics"]) == (4, 2, 2, 1, 2, 1, 2, 3)
    assert s["productScore"] == 95.0 and s["outletScore"] == 80.0
    g = {x["code"]: x for x in o["groups"]}
    assert g["CTG"]["outlets"] == 2 and g["CTG"]["visited"] == 1 and g["CTG"]["productScore"] == 90.0
    assert g["CCI"]["visited"] == 1 and g["—"]["outlets"] == 1 and g["—"]["visited"] == 0  # without a group
    assert [t["date"] for t in o["trend"]] == ["2026-10-20", "2026-10-21"] and o["trend"][0]["checkins"] == 1
    # the assessment per PIC: own visits and the SWAT agreement; per outlet the SWAT audits and open action plans
    pics = {p["id"]: p for p in o["picAssessment"]}
    assert (pics["u1"]["productScore"], pics["u1"]["outletScore"], pics["u1"]["conformity"], pics["u1"]["overrated"], pics["u1"]["openActions"]) == (90.0, 80.0, 75.0, 3, 2)
    assert pics["u2"]["checkinDays"] == 1 and pics["u2"]["conformity"] is None
    # the PIC score: check-ins (1 of 1 outlet × 18 working days of 21), punctual (09:00 WIB), visits, scores, SWAT
    assert pics["u1"]["checkinTarget"] == 18 and pics["u1"]["punctuality"] == 100.0 and pics["u1"]["score"] is not None
    assert mo._score({"checkin": 50.0, "punctuality": None, "visits": None, "product": None, "outlet": None, "swat": 80.0}) == round((50 * 15 + 80 * 30) / 45, 1)
    ctg = next(b for b in o["branches"] if b["code"] == "CTG54")
    assert (ctg["swatAudits"], ctg["swatScore"], ctg["openActions"]) == (1, 88.0, 2) and o["swat"]["openActions"] == 2
    only = mo.overview(date(2026, 10, 1), date(2026, 10, 21), None, "CTG")
    assert only["summary"]["outlets"] == 2 and [x["code"] for x in only["groups"]] == ["CTG"]
    scoped = mo.overview(date(2026, 10, 1), date(2026, 10, 21), ["CCI01"], None)
    assert scoped["summary"]["outlets"] == 1 and scoped["summary"]["checkins"] == 0


def test_group_codes_are_checked():
    for bad in ("", "cci with space", "X" * 21):
        with pytest.raises(bg.GroupError):
            bg._clean(bad, "Nama")
    assert bg._clean("ctgp", "  To   Go  ") == ("CTGP", "To Go")


def test_pages_follow_access(anon, store, page_access, monkeypatch, sources):
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/monitoring/overview").status_code == 403
    assert anon.get("/api/monitoring/branch-groups").status_code == 403
    page_access.overrides[str(store.user["id"])] = {"mon.overview": True}
    assert anon.get("/api/monitoring/overview").status_code == 200
    assert anon.get("/api/monitoring/branch-groups").status_code == 200  # read by any monitoring page (filters)
    assert anon.post("/api/monitoring/branch-groups", json={"code": "X", "name": "X"}).status_code == 403
