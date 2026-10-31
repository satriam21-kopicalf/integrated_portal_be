"""Waste (app/waste.py, app/routes/waste.py): access follows the pages, every item needs a category and a photo, the
author deletes an entry until it is reviewed, Cost Control reviews many at once."""
from datetime import date, datetime, timezone

from app import access, waste
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)
from tests.test_pic_checkin import _jpeg

KEMANG = {"code": "CCI01", "name": "Kopi Calf Kemang", "latitude": None, "longitude": None, "radius_m": None}
CATALOG = {"items": {"waste": [{"code": "RMFR-00003", "name": "DIMSUM", "unit": "PCS"}], "usage": [],
                     "premix": [{"code": "PREMIX-MAGIC", "name": "PREMIX MAGIC", "unit": "GR", "type": "PREMIX"}]},
           "categories": ["Packaging Rusak", "Premix Expired/ Over Stock"], "scopes": waste.SCOPES}


def _row(**kw):
    base = {"id": "22222222-2222-2222-2222-222222222222", "user_id": "u", "username": "kasir", "full_name": "Kasir",
            "role": "user", "branch_code": "CCI01", "branch_name": "Kopi Calf Kemang", "business_date": date(2026, 10, 31),
            "scope": "premix", "item_code": "PREMIX-MAGIC", "item_name": "PREMIX MAGIC", "unit": "GR", "qty": 250,
            "category": "Premix Expired/ Over Stock", "note": None, "photo_taken_at": None, "latitude": None,
            "longitude": None, "status": "submitted", "review_note": None, "reviewed_by": None, "reviewed_at": None,
            "created_at": datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc)}
    return {**base, **kw}


def test_waste_pages_in_the_catalog():
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.PAGE_KEYS)
    assert {"cc.waste-monitor", "cc.waste-review"} <= set(access.CC_PAGES)
    assert "waste" in access.DEFAULT_ROLE_PAGES["user"] and "waste" in access.DEFAULT_ROLE_PAGES["outlet"]
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.DEFAULT_ROLE_PAGES["cost_control"])


def test_entry_needs_category_photo_and_a_known_item(anon, store, fake_db, page_access, monkeypatch):
    monkeypatch.setattr(waste, "branch_options", lambda user, every=False: [KEMANG])
    monkeypatch.setattr(waste, "catalog", lambda: CATALOG)
    monkeypatch.setattr(waste, "today", lambda: date(2026, 10, 31))
    saved = {}

    def insert(query, params=None):
        saved.update(params)
        return _row(qty=params["q"], note=params["n"])

    monkeypatch.setattr(waste.db, "fetchrow", insert)
    body = {"branchCode": "CCI01", "businessDate": "2026-10-31", "scope": "premix", "itemCode": "PREMIX-MAGIC",
            "qty": 250, "category": "Premix Expired/ Over Stock", "note": "tumpah", "photo": _jpeg()}
    login(anon, "kasir", "Kasir1234")  # role user (a PIC): the waste input is a page of the role, Cost Control is not
    assert anon.get("/api/waste/monitor").status_code == 403 and anon.get("/api/waste/reviews").status_code == 403
    for bad, msg in (({"category": "Lainnya"}, "kategori"), ({"itemCode": "NOPE"}, "Item tidak ada"),
                     ({"branchCode": "CCI99"}, "Cabang"), ({"businessDate": "2026-10-28"}, "hari ini atau kemarin")):
        r = anon.post("/api/waste/entries", json={**body, **bad})
        assert r.status_code == 422 and msg in r.json()["error"], (bad, r.json())
    assert anon.post("/api/waste/entries", json={**body, "photo": ""}).status_code == 422  # photo required
    assert not saved
    r = anon.post("/api/waste/entries", json=body)
    assert r.status_code == 201 and r.json()["scope"] == "premix" and saved["unit"] == "GR" and saved["n"] == "tumpah"


def test_monitor_and_review(anon, store, fake_db, page_access, monkeypatch):
    calls = []
    monkeypatch.setattr(waste, "catalog", lambda: CATALOG)
    monkeypatch.setattr(waste.db, "fetch", lambda q, p=None: calls.append(q) or ([_row(id="a"), _row(id="b")]
                                                                                if "RETURNING" in q else []))
    monkeypatch.setattr(waste.db, "fetchrow", lambda q, p=None: {"entries": 0})
    login(anon, "kasir", "Kasir1234")
    page_access.overrides[str(store.user["id"])] = {"cc.waste-monitor": True, "cc.waste-review": True}
    m = anon.get("/api/waste/monitor", params={"scope": "premix"})
    assert m.status_code == 200 and m.json()["categories"] == CATALOG["categories"]
    assert anon.get("/api/waste/reviews").status_code == 200
    r = anon.post("/api/waste/reviews", json={"ids": ["a", "b"], "status": "approved"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert anon.post("/api/waste/reviews", json={"ids": ["a"], "status": "maybe"}).status_code == 422


def test_surveyor_check_in_is_given_per_account(anon, store, page_access):
    assert "form.surveyor-check-in" in access.PAGE_KEYS and "mon.surveyor-check-in" in access.PAGE_KEYS
    assert "form.surveyor-check-in" not in access.DEFAULT_ROLE_PAGES["user"]  # per user (migration 039), not per role
    assert "form.surveyor-check-in" in access.OPERATIONS_PAGES  # the surveyors of role user sign in to Operations
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/forms/surveyor-check-in").status_code == 403
    assert anon.get("/api/monitoring/surveyor-check-in").status_code == 403


def test_surveyor_sees_every_branch(anon, store, page_access, monkeypatch):
    from app import pic_checkin

    seen = {}
    monkeypatch.setattr(pic_checkin, "branch_options", lambda user, every=False: seen.update(every=every) or [KEMANG])
    monkeypatch.setattr(pic_checkin, "recent_of", lambda uid, limit=10, kind="pic": seen.update(kind=kind) or [])
    monkeypatch.setattr(pic_checkin, "done_today", lambda uid, kind="pic": {})
    login(anon, "kasir", "Kasir1234")
    page_access.overrides[str(store.user["id"])] = {"form.surveyor-check-in": True}
    r = anon.get("/api/forms/surveyor-check-in")
    assert r.status_code == 200 and seen == {"every": True, "kind": "surveyor"}
    assert anon.get("/api/forms/pic-check-in").status_code == 200 and seen["every"] is False
