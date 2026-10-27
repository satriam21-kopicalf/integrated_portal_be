"""Rating & Review (app/routes/rating.py, app/gmaps.py): each source opens with its own page, off by default; Google Maps
matches the outlets to their place and reads the latest Places API data."""
from datetime import datetime

import pytest

from app import access, gmaps
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

SOURCES = ("google-maps", "shopeefood", "gofood", "grabfood")
WIB = gmaps.WIB


@pytest.fixture
def places(monkeypatch):
    """Outlets and the gmaps_place table in memory; the Places API answers from `api`."""
    state = {"outlets": [{"code": "CCI01", "name": "Kopi Calf Kemang", "latitude": -6.26, "longitude": 106.81},
                         {"code": "CCI02", "name": "Kopi Calf Jatiwaringin", "latitude": None, "longitude": None}],
             "rows": {}, "search": [], "details": {}, "calls": []}
    monkeypatch.setattr(gmaps, "outlets", lambda: state["outlets"])
    monkeypatch.setattr(gmaps, "_rows", lambda codes=None: {k: dict(v) for k, v in state["rows"].items() if codes is None or k in codes})

    def save(code, name, **cols):
        row = state["rows"].setdefault(code, {"branch_code": code, "status": "pending", "place_id": None, "matched_at": None})
        row.update(branch_name=name, **cols)
    monkeypatch.setattr(gmaps, "_save", save)

    def call(method, path, fields, body=None, params=None):
        state["calls"].append(path)
        if path == "places:searchText":
            return {"places": state["search"]}
        return state["details"][path.split("/", 1)[1]]
    monkeypatch.setattr(gmaps, "_call", call)
    monkeypatch.setattr(gmaps.get_settings(), "google_places_api_key", "test-key")
    return state


def place(pid, name, lat, lng):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": "Jl. Test", "location": {"latitude": lat, "longitude": lng}}


def test_rating_pages_off_by_default():
    assert {f"rating.{s}" for s in SOURCES} <= set(access.PAGE_KEYS)
    for pages in access.DEFAULT_ROLE_PAGES.values():
        assert not any(p.startswith("rating.") for p in pages)


def test_rating_endpoints_follow_their_pages(anon, store, page_access, places):
    login(anon, "kasir", "Kasir1234")  # role user, branch CCI01
    assert all(anon.get(f"/api/rating/{s}").status_code == 403 for s in SOURCES)
    page_access.overrides[str(store.user["id"])] = {"rating.gofood": True, "rating.google-maps": True}
    r = anon.get("/api/rating/gofood")
    assert r.status_code == 200 and r.json()["branches"] == ["CCI01"]
    assert anon.get("/api/rating/grabfood").status_code == 403
    assert anon.get("/api/rating/tiktok").status_code == 404
    g = anon.get("/api/rating/google-maps").json()
    assert [o["code"] for o in g["data"]] == ["CCI01"] and g["canEdit"] is False  # own branch only
    assert anon.post("/api/rating/google-maps/sync").status_code == 403  # role user does not edit
    assert anon.put("/api/rating/google-maps/places/CCI01", json={"placeId": None}).status_code == 403
    login(anon, "superadmin", "Admin1234")
    assert all(anon.get(f"/api/rating/{s}").status_code == 200 for s in SOURCES)
    assert len(anon.get("/api/rating/google-maps").json()["data"]) == 2


def test_match_near_outlet_or_flag_for_review(places):
    places["search"] = [place("far", "Kopi Calf Kemang", -6.27, 106.81), place("near", "Kopi Calf - Kemang", -6.2605, 106.8101),
                        place("other", "Kopi Kenangan Kemang", -6.26, 106.81)]
    m = gmaps.match(places["outlets"][0])
    assert m["place_id"] == "near" and m["status"] == "auto" and m["distance"] < 100
    places["search"] = [place("far", "Kopi Calf Kemang", -6.27, 106.81)]
    assert gmaps.match(places["outlets"][0])["status"] == "review"  # ~1.1 km away, same name
    assert gmaps.match(places["outlets"][0], frozenset({"far"}))["status"] == "missing"  # taken by another outlet
    assert gmaps.match(places["outlets"][1])["status"] == "missing"  # no coordinates and another name
    places["search"] = [place("jw", "Kopi Calf To Go Jatiwaringin", -6.27, 106.9)]
    assert gmaps.match(places["outlets"][1]) == {"place_id": "jw", "status": "review", "distance": None}
    places["search"] = [place("other", "Kopi Kenangan Kemang", -6.26, 106.81)]
    assert gmaps.match(places["outlets"][0])["status"] == "missing"


def test_sync_matches_once_then_refreshes(places):
    places["search"] = [place("p1", "Kopi Calf Kemang", -6.26, 106.81)]
    places["details"] = {"p1": {"id": "p1", "rating": 4.7, "userRatingCount": 619}}
    stats = gmaps.sync()
    assert stats["refreshed"] == 1 and places["rows"]["CCI01"]["data"]["rating"] == 4.7
    assert places["rows"]["CCI02"]["status"] == "missing"
    places["calls"].clear()
    gmaps.sync()
    assert "places:searchText" not in places["calls"]  # matched places are not searched again
    places["rows"]["CCI02"].update(place_id=None, status="none")
    places["calls"].clear()
    gmaps.sync(branch="CCI02")
    assert places["calls"] == []  # "not on Google Maps" is left alone


def test_open_now_from_regular_hours():
    daily = [{"open": {"day": d, "hour": 7, "minute": 0}, "close": {"day": d, "hour": 22, "minute": 0}} for d in range(7)]
    monday_9 = datetime(2026, 10, 19, 9, 0, tzinfo=WIB)
    assert gmaps.is_open(daily, monday_9) is True
    assert gmaps.is_open(daily, datetime(2026, 10, 19, 23, 0, tzinfo=WIB)) is False
    late = [{"open": {"day": 6, "hour": 20, "minute": 0}, "close": {"day": 0, "hour": 2, "minute": 0}}]  # Sat 20:00 → Sun 02:00
    assert gmaps.is_open(late, datetime(2026, 10, 18, 1, 0, tzinfo=WIB)) is True  # Sunday 01:00
    assert gmaps.is_open([{"open": {"day": 0, "hour": 0, "minute": 0}}], monday_9) is True  # 24 hours
    assert gmaps.is_open(None, monday_9) is None
    week = [f"{d}: 07.00–22.00" for d in ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")]
    assert gmaps.today_hours(week, monday_9) == "07.00–22.00"


def test_one_place_one_outlet(places):
    """Two outlets on one place: the nearer one with the same name keeps it, the other searches again without it."""
    places["outlets"] = [{"code": "A", "name": "Kopi Calf To Go Depok Beji", "latitude": -6.37, "longitude": 106.82},
                         {"code": "B", "name": "Kopi Calf To Go Sawangan Depok", "latitude": -6.40, "longitude": 106.78}]
    places["search"] = [place("beji", "Kopi Calf To Go Depok Beji", -6.3705, 106.8201)]
    places["details"] = {"beji": {"id": "beji", "displayName": {"text": "Kopi Calf To Go Depok Beji"}}}
    gmaps.sync()
    assert places["rows"]["A"]["place_id"] == "beji" and places["rows"]["A"]["status"] == "auto"
    assert places["rows"]["B"]["place_id"] is None and places["rows"]["B"]["status"] == "missing"


def test_reviews_newest_first(places):
    places["rows"]["CCI01"] = {"branch_code": "CCI01", "status": "auto", "place_id": "p1", "data": {"reviews": [
        {"rating": 5, "publishTime": "2024-06-29T10:00:00Z"}, {"rating": 4, "publishTime": "2026-08-08T10:00:00Z"},
        {"rating": 3, "publishTime": "2025-05-12T10:00:00Z"}]}}
    got = next(o for o in gmaps.overview() if o["code"] == "CCI01")["reviews"]
    assert [r["publishedAt"][:4] for r in got] == ["2026", "2025", "2024"]


def test_grabfood_session_is_super_admin_only(anon, store, page_access):
    """Cost Control and branch users see the GrabFood data of their scope but never the merchant session (2026-10-27)."""
    store.add("cc", "cc@kopicalf.co.id", "Cost Control", "Cost1234", role="cost_control")
    page_access.overrides[str(store.user["id"])] = {"rating.grabfood": True}
    cc = next(u for u in store.users.values() if u["username"] == "cc")
    page_access.overrides[str(cc["id"])] = {"rating.grabfood": True}
    cookie = {"cookie": "mexusers_authn_token=abc.def.ghi; mexusers_authn_token_jti=xyz"}
    for name, pw in (("kasir", "Kasir1234"), ("cc", "Cost1234")):
        login(anon, name, pw)
        g = anon.get("/api/rating/grabfood").json()
        assert g["canEdit"] is False and g["session"] is None
        assert anon.put("/api/rating/grabfood/session", json=cookie).status_code == 403
        r = anon.get("/api/rating/grabfood/reviews?rating=1,2&photo=true&page=1&size=10").json()
        assert r["data"] == [] and r["total"] == 0 and r["size"] == 10
        assert anon.get("/api/rating/grabfood/reviews/unknown").status_code == 404
        assert anon.get("/api/rating/grabfood/reviews?rating=9").status_code == 422
    login(anon, "superadmin", "Admin1234")
    g = anon.get("/api/rating/grabfood").json()
    assert g["canEdit"] is True and g["session"] == {"present": False, "valid": False}


def test_grabfood_reviews_follow_the_branch_scope(fake_db):
    """A branch user only reads reviews of the outlets mapped to their branches; an unmapped outlet needs every branch."""
    from app import grab
    grab.reviews(("CCI01",), photo=True, replied=False, q="tumpah_%", ratings=[1, 2])
    sql, args = fake_db.queries[-1]
    assert "s.branch_code = ANY(%s)" in sql and ["CCI01"] in args and [1, 2] in args
    assert r"%tumpah\_\%%" in args  # the search text is a literal, not a pattern
    grab.reviews((), page=1)
    assert "s.branch_code = '-'" in fake_db.queries[-1][0]


def test_grabfood_menu_availability(anon, store, page_access, fake_db):
    """The menu endpoint follows the page and the branch scope; the default lists only what is unavailable."""
    page_access.overrides[str(store.user["id"])] = {"rating.grabfood": True}
    login(anon, "kasir", "Kasir1234")
    r = anon.get("/api/rating/grabfood/menu").json()
    assert r == {"data": [], "counts": {"AVAILABLE": 0, "OUT_OF_STOCK": 0, "OFF": 0}}
    sql, args = fake_db.queries[-1]
    assert "m.status <> 'AVAILABLE'" in sql and ["CCI01"] in args
    assert anon.get("/api/rating/grabfood/menu?status=SOLD").status_code == 422


def test_grabfood_branch_mapping_is_super_admin_only(anon, store, page_access):
    page_access.overrides[str(store.user["id"])] = {"rating.grabfood": True}
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/rating/grabfood/branches").status_code == 403
    assert anon.put("/api/rating/grabfood/stores/6-X/branch", json={"branchCode": "CCI01"}).status_code == 403
    login(anon, "superadmin", "Admin1234")
    assert anon.get("/api/rating/grabfood/branches").json() == {"data": []}
    assert anon.put("/api/rating/grabfood/stores/6-X/branch", json={"branchCode": "CCI01"}).status_code == 404  # unknown store
    assert anon.put("/api/rating/grabfood/stores/6-X/branch", json={"branchCode": "bad code!"}).status_code == 422


def test_grabfood_opening_hours_and_minutes_closed():
    """Grab's weekly hours are Sunday first; a range past midnight stops at 24:00; minutes closed only count inside the
    opening hours, and before the first known status the outlet counts as open."""
    from datetime import date, datetime, timedelta

    from app.grab import WIB, _hours_view, closed_minutes, day_ranges
    week = [{"ranges": [{"start": "08:00", "end": "20:00"}]}] * 5 + \
        [{"ranges": [{"start": "08:00", "end": "11:45"}, {"start": "12:30", "end": "20:00"}]},
         {"ranges": [{"start": "18:00", "end": "02:00"}]}]
    assert day_ranges(week, date(2026, 10, 30)) == [(480, 705), (750, 1200)]  # Friday: prayer break
    assert day_ranges(week, date(2026, 10, 31)) == [(1080, 1440)]             # Saturday: past midnight, cut at 24:00
    assert day_ranges(None, date(2026, 10, 30)) == []
    day = datetime(2026, 10, 27, tzinfo=WIB)                                   # Tuesday 08:00-20:00
    events = [(day - timedelta(hours=3), True), (day + timedelta(hours=9), False), (day + timedelta(hours=10, minutes=30), True),
              (day + timedelta(hours=14), False)]
    assert closed_minutes(day_ranges(week, day.date()), events, day, day + timedelta(hours=15)) == 150
    assert closed_minutes(day_ranges(week, day.date()), [], day, day + timedelta(hours=15)) == 0  # no data, no blame
    v = _hours_view(week, events, False, day + timedelta(hours=15))
    assert v["shouldBeOpen"] is True and v["closedInHours"] is True and v["closedMinutesToday"] == 150
    assert v["today"] == [{"start": "08:00", "end": "20:00"}] and len(v["week"]) == 7
    assert _hours_view(None, [], False, day)["shouldBeOpen"] is None


def test_grabfood_reviews_city_text_and_sort(fake_db):
    from app import grab
    grab.reviews(None, city="Bandung", text=True, sort="lowest")
    sql, args = fake_db.queries[-1]
    assert "s.city_name = %s" in sql and "Bandung" in args and "btrim(gr.description), '') <> ''" in sql
    assert "ORDER BY gr.rating NULLS LAST" in sql
    grab.reviews(None, sort="bad")  # an unknown sort falls back to newest
    assert "ORDER BY gr.created_at DESC" in fake_db.queries[-1][0]
