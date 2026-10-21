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
        {"branchCode": "CTG54", "userId": "u1", "outsideRadius": False, "visitedAt": "2026-10-20T02:00:00+00:00"},
        {"branchCode": "CTG54", "userId": "u2", "outsideRadius": True, "visitedAt": "2026-10-21T02:00:00+00:00"}]})

    def monitor(form, a, b, branches, limit=0):
        data = [{"branchCode": "CTG54", "userId": "u1", "score": 90.0 if form == "product" else 80.0,
                 "counts": {"under": 2 if form == "outlet" else 0}, "submittedAt": "2026-10-20T03:00:00+00:00"}]
        if form == "product":
            data.append({"branchCode": "CCI01", "userId": "u3", "score": 100.0, "counts": {"under": 0}, "submittedAt": "2026-10-21T03:00:00+00:00"})
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
