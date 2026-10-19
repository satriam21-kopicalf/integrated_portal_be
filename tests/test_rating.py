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
    assert gmaps.match(places["outlets"][0])["status"] == "review"  # ~1.1 km away
    assert gmaps.match(places["outlets"][1]) == {"place_id": "far", "status": "review", "distance": None}  # no coordinates
    places["search"] = [place("other", "Kopi Kenangan Kemang", -6.26, 106.81)]
    assert gmaps.match(places["outlets"][0])["status"] == "missing"


def test_sync_matches_once_then_refreshes(places):
    places["search"] = [place("p1", "Kopi Calf Kemang", -6.26, 106.81)]
    places["details"] = {"p1": {"id": "p1", "rating": 4.7, "userRatingCount": 619}}
    stats = gmaps.sync()
    assert stats["refreshed"] == 2 and places["rows"]["CCI01"]["data"]["rating"] == 4.7
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
