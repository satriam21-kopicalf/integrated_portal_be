"""Branch groups and Monitoring → Overview (app/branch_groups.py, app/monitoring_overview.py)."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from decimal import Decimal

import pytest

from app import branch_groups as bg
from app import form_scope
from app import monitoring_overview as mo
from app import pic_settings as ps
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
    # CTG: on time until 10:30 WIB; the net sales of CTG54 at 80 % of its target
    monkeypatch.setattr(mo.ps, "deadlines", lambda codes: {c: {"deadline": time(10, 30), "zone": ZoneInfo("Asia/Jakarta"), "source": "group"} for c in codes})
    monkeypatch.setattr(mo.ps, "sales_scores", lambda codes, a, b: {c: (80.0 if c == "CTG54" else None) for c in codes})

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
    # the PIC score: check-ins (1 of 1 outlet × 18 working days of 21: 21 − ⌊21 / 7⌋), punctual (09:00 WIB by 10:30),
    # Product / Outlet (done against the target and the score), SWAT and net sales
    u1 = pics["u1"]
    assert u1["checkinTarget"] == 18 and u1["punctuality"] == 100.0 and u1["punctualityLegacy"] == 100.0
    assert u1["productDone"] == round(100 / 18, 1) and u1["productPart"] == round((round(100 / 18, 1) + 90.0) / 2, 1)
    assert u1["salesScore"] == 80.0 and u1["salesOutlets"] == 1 and u1["score"] is not None
    assert mo._score({"checkin": 50.0, "punctuality": None, "product": None, "outlet": None, "swat": 80.0, "sales": None}) == round((50 * 15 + 80 * 15) / 30, 1)
    assert mo._score({"checkin": 100.0, "punctuality": 100.0, "product": 100.0, "outlet": 100.0, "swat": 100.0, "sales": 0.0}) == 75.0
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


def test_working_days_follow_the_6_1_schedule():
    assert [ps.working_days(n) for n in (28, 29, 30, 31, 7, 6, 0)] == [24, 25, 26, 27, 6, 6, 0]


def test_check_in_deadline_on_the_outlet_clock():
    wib = {"deadline": time(10, 30), "zone": ZoneInfo("Asia/Jakarta"), "source": "group"}
    wita = {"deadline": time(10, 30), "zone": ZoneInfo("Asia/Makassar"), "source": "outlet"}
    at = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)  # 09:00 WIB = 10:00 WITA
    assert ps.on_time(at, wib) and ps.on_time(at, wita)
    late = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)  # 10:00 WIB = 11:00 WITA: late in Bali only
    assert ps.on_time(late, wib) and not ps.on_time(late, wita)
    night = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)  # 01:00 WIB: not on time (the workbook counted it)
    assert not ps.on_time(night, wib)
    with pytest.raises(ps.SettingError):
        ps.group_deadline("25:00")
    assert ps.group_deadline("10:15") == time(10, 15) and ps.group_deadline(None) is None


def test_form_items_per_group(monkeypatch):
    rows = {("product", "a.1"): {"CTG": False}, ("product", "a.2"): {"CTGP": True}}
    monkeypatch.setattr(form_scope, "_load", lambda: (rows, ["CCI", "CTG", "CTGP"]))
    assert form_scope.effective_only_for("product", "a.1", None) == ["CCI", "CTGP"]  # hidden at CTG
    assert form_scope.effective_only_for("product", "a.2", ["CCI"]) == ["CCI", "CTGP"]  # also asked at CTGP
    assert form_scope.effective_only_for("product", "a.3", ["CCI"]) == ["CCI"]  # untouched
    d = {"groups": [{"sections": [{"items": [{"key": "a.1"}, {"key": "a.2", "onlyFor": ["CCI"]}]}]}]}
    out = form_scope.apply("product", d)
    assert out["groups"][0]["sections"][0]["items"][0]["onlyFor"] == ["CCI", "CTGP"] and "onlyFor" not in d["groups"][0]["sections"][0]["items"][0]
    w = {"groups": [{"sections": [{"items": [{"key": "product:a.1"}]}]}]}
    assert form_scope.apply("wwwr", w)["groups"][0]["sections"][0]["items"][0]["onlyFor"] == ["CCI", "CTGP"]  # WW-WR too


def test_net_sales_against_the_outlets_own_month_before(monkeypatch):
    seen = []

    def fake(codes, a, b):
        seen.append((a, b))
        if a.month == 11:  # the period: A sells 90 a day, B 130 a day, C nothing yet
            return {"A": (Decimal(900), 10), "B": (Decimal(1300), 10)}
        return {"A": (Decimal(7200), 72), "B": (Decimal(7200), 72), "C": (Decimal(500), 5)}  # 100 a day before

    monkeypatch.setattr(ps, "daily_sales", fake)
    assert ps.sales_scores(["A", "B", "C", "D"], date(2026, 11, 1), date(2026, 11, 10)) == {"A": 90.0, "B": 100.0, "C": None, "D": None}
    assert seen[1] == (date(2026, 10, 1), date(2026, 10, 31))  # the full month before
    assert ps._baseline_window(date(2026, 1, 15)) == (date(2025, 12, 1), date(2025, 12, 31))  # over the new year
