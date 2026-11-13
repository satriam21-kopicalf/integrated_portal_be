"""Waste (app/waste.py, app/routes/waste.py): access follows the pages, every item needs a category and a photo, the
author deletes an entry until it is reviewed, Cost Control reviews many at once."""
import pytest
from datetime import date, datetime, timezone

from app import access, waste
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)
from tests.test_pic_checkin import _jpeg

KEMANG = {"code": "CCI01", "name": "Kopi Calf Kemang", "latitude": None, "longitude": None, "radius_m": None}
CATALOG = {"items": {"waste": [{"code": "RMFR-00003", "name": "DIMSUM", "unit": "PCS"}], "usage": [],
                     "premix": [{"code": "PREMIX-MAGIC", "name": "PREMIX MAGIC", "unit": "GR", "type": "PREMIX"}]},
           "categories": ["Packaging Rusak", "Premix Expired/ Over Stock"], "usageCategories": ["Customer Supplies"],
           "scopes": waste.SCOPES}


def _row(**kw):
    base = {"id": "22222222-2222-2222-2222-222222222222", "user_id": "u", "username": "kasir", "full_name": "Kasir",
            "role": "user", "branch_code": "CCI01", "branch_name": "Kopi Calf Kemang", "business_date": date(2026, 10, 31),
            "scope": "premix", "item_code": "PREMIX-MAGIC", "item_name": "PREMIX MAGIC", "unit": "GR", "qty": 250,
            "category": "Premix Expired/ Over Stock", "note": None, "photo_taken_at": None, "latitude": None,
            "longitude": None, "status": "submitted", "review_note": None, "reviewed_by": None, "reviewed_at": None,
            "created_at": datetime(2026, 10, 31, 3, 0, tzinfo=timezone.utc), "production_date": None, "expired_date": None}
    return {**base, **kw}


def test_waste_pages_in_the_catalog():
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.PAGE_KEYS)
    assert {"cc.waste-monitor", "cc.waste-review"} <= set(access.CC_PAGES)
    assert "waste" in access.DEFAULT_ROLE_PAGES["user"] and "waste" in access.DEFAULT_ROLE_PAGES["outlet"]
    assert {"waste", "cc.waste-monitor", "cc.waste-review"} <= set(access.DEFAULT_ROLE_PAGES["cost_control"])


def test_entry_needs_category_photo_and_a_known_item(anon, store, fake_db, page_access, monkeypatch):
    monkeypatch.setattr(waste, "branch_options", lambda user, every=False: [KEMANG])
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: CATALOG)
    monkeypatch.setattr(waste, "today", lambda: date(2026, 10, 31))
    monkeypatch.setattr(waste, "facility_type", lambda code: "OUTLET")
    saved = {}

    def insert(query, params=None):
        saved.update(params)
        return _row(qty=params["q"], note=params["n"])

    monkeypatch.setattr(waste.db, "fetchrow", insert)
    body = {"branchCode": "CCI01", "businessDate": "2026-10-31", "scope": "premix", "itemCode": "PREMIX-MAGIC",
            "qty": 250, "category": "Premix Expired/ Over Stock", "note": "tumpah", "photo": _jpeg(),
            "productionDate": "2026-10-25", "expiredDate": "2026-11-05", "signature": _jpeg()}
    login(anon, "kasir", "Kasir1234")  # role user (a PIC): the waste input is a page of the role, Cost Control is not
    assert anon.get("/api/waste/monitor").status_code == 403 and anon.get("/api/waste/reviews").status_code == 403
    for bad, msg in (({"category": "Lainnya"}, "kategori"), ({"itemCode": "NOPE"}, "Item tidak ada"),
                     ({"branchCode": "CCI99"}, "Cabang"), ({"businessDate": "2026-10-28"}, "hari ini atau kemarin"),
                     ({"productionDate": "2026-10-31", "expiredDate": "2026-10-01"}, "Expired date"),
                     ({"note": None}, "Catatan / alasan"), ({"note": "  "}, "Catatan / alasan")):  # 2026-11-13: required
        r = anon.post("/api/waste/entries", json={**body, **bad})
        assert r.status_code == 422 and msg in r.json()["error"], (bad, r.json())
    assert anon.post("/api/waste/entries", json={**body, "photo": ""}).status_code == 422  # photo required
    r = anon.post("/api/waste/entries", json={**body, "signature": None})  # signed before submitting (migration 055)
    assert r.status_code == 422 and "Tanda tangan" in r.json()["error"]
    assert not saved
    r = anon.post("/api/waste/entries", json=body)
    assert r.status_code == 201 and r.json()["scope"] == "premix" and saved["unit"] == "GR" and saved["g"] == 250 and saved["n"] == "tumpah" and str(saved["ed"]) == "2026-11-05"


def test_monitor_and_review(anon, store, fake_db, page_access, monkeypatch):
    calls = []
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: CATALOG)
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


class _Conn:
    def __init__(self):
        self.sql = []

    def execute(self, query, params=None):
        self.sql.append((query, params))


def _tx(monkeypatch):
    from contextlib import contextmanager
    conn = _Conn()
    monkeypatch.setattr(waste.db, "transaction", contextmanager(lambda timeout_ms=None: (yield conn)))
    return conn


def test_usage_list_and_no_waste(anon, store, fake_db, page_access, monkeypatch):
    monkeypatch.setattr(waste, "branch_options", lambda user, every=False: [KEMANG])
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: {**CATALOG, "items": {**CATALOG["items"], "usage": [
        {"code": "INSU-00005", "name": "CABLE TIES", "unit": "PACK"}, {"code": "AIIR-00001", "name": "AIR", "unit": "GR"}]}})
    monkeypatch.setattr(waste, "today", lambda: date(2026, 10, 31))
    conn = _tx(monkeypatch)
    login(anon, "kasir", "Kasir1234")
    monkeypatch.setattr(waste.db, "fetch", lambda q, p=None: [])  # no photo saved yet
    line = {"qty": 2, "category": "Customer Supplies", "note": "sobek", "photo": _jpeg()}
    body = {"branchCode": "CCI01", "businessDate": "2026-10-31", "lines": {"INSU-00005": line, "AIIR-00001": None},
            "signature": _jpeg()}
    # the categories of Usage Management are its own (migration 055), and the list is signed
    r = anon.put("/api/waste/usage", json={**body, "lines": {"INSU-00005": {**line, "category": "Packaging Rusak"}}})
    assert r.status_code == 422 and "kategori" in r.json()["error"]
    r = anon.put("/api/waste/usage", json={**body, "signature": None})
    assert r.status_code == 422 and "Tanda tangan" in r.json()["error"]
    assert anon.put("/api/waste/usage", json={**body, "lines": {"NOPE": {"qty": 1}}}).status_code == 422
    # every SKU needs its category and a photo (migration 045)
    r = anon.put("/api/waste/usage", json={**body, "lines": {"INSU-00005": {**line, "category": ""}}})
    assert r.status_code == 422 and "kategori" in r.json()["error"]
    r = anon.put("/api/waste/usage", json={**body, "lines": {"INSU-00005": {**line, "photo": None}}})
    assert r.status_code == 422 and "Foto" in r.json()["error"]
    assert anon.put("/api/waste/usage", json={**body, "businessDate": "2026-10-20"}).status_code == 422
    r = anon.put("/api/waste/usage", json=body)
    assert r.status_code == 200 and r.json()["saved"] == 1 and r.json()["removed"] == 1
    assert any("DELETE" in q for q, _ in conn.sql) and any("ON CONFLICT" in q for q, _ in conn.sql)
    # usage is the daily list now, not a single waste entry
    assert anon.post("/api/waste/entries", json={"branchCode": "CCI01", "scope": "usage", "itemCode": "INSU-00005",
                                                 "qty": 1, "category": "Packaging Rusak", "photo": _jpeg()}).status_code == 422
    r = anon.put("/api/waste/no-waste", json={"branchCode": "CCI01", "noWaste": True})
    assert r.status_code == 200 and r.json()["noWaste"] is True


def test_daily_lists_branches_not_reported(monkeypatch):
    other = {**KEMANG, "code": "CCI02", "name": "Kopi Calf Senopati"}
    monkeypatch.setattr(waste, "branch_options", lambda user, every=False: [KEMANG, other])
    monkeypatch.setattr(waste, "today", lambda: date(2026, 10, 31))
    d = date(2026, 10, 31)

    closed = {**KEMANG, "code": "TGO99", "name": "Kopi Calf To Go Tutup"}
    monkeypatch.setattr(waste, "branch_options", lambda user, every=False: [KEMANG, other, closed])

    def fetch(q, p=None):
        if "agg_sales_daily" in q:  # the branches open in the period: the closed outlet sold nothing
            return [{"branch_code": "CCI01"}, {"branch_code": "CCI02"}]
        if "waste_day" in q:
            return [{"branch_code": "CCI02", "business_date": date(2026, 10, 30)}]
        if "usage_count" in q:
            return [{"branch_code": "CCI01", "business_date": d, "n": 3}]
        return [{"branch_code": "CCI01", "business_date": d, "n": 2}]
    monkeypatch.setattr(waste.db, "fetch", fetch)
    out = waste.daily(date(2026, 10, 30), date(2026, 11, 2), None)
    assert out["dates"] == ["2026-10-30", "2026-10-31"]  # never past today
    kemang, seno = out["branches"]
    assert kemang["reported"] == 1 and kemang["usageDays"] == 1 and seno["reported"] == 1
    assert out["missingLast"] == [{"code": "CCI02", "name": "Kopi Calf Senopati"}]


def test_premix_is_entered_like_the_stock_opname_wip():
    """Count unit Porsi (115 g), or grams; the ingredient grams follow the recipe and are rounded up per ingredient."""
    item = {"code": "BASE-PUDING", "name": "BASE PUDING", "unit": "Porsi",
            "units": [{"uom": "Porsi", "grams": 115.0}, {"uom": "Gram", "grams": 1.0}],
            "components": [{"code": "RMDA-1", "name": "SUSU", "isStock": True, "perGram": 0.6},
                           {"code": None, "name": "Air", "isStock": False, "perGram": 0.4}]}
    unit, grams, comps = waste.explode(item, None, waste.Decimal("3"))
    assert unit == "Porsi" and grams == 345 and [c["grams"] for c in comps] == [207, 138]
    unit, grams, comps = waste.explode(item, "gram", waste.Decimal("10.5"))
    assert unit == "Gram" and grams == waste.Decimal("10.5") and comps[0]["grams"] == 7  # 6.3 g → 7
    try:
        waste.explode(item, "Pail", waste.Decimal(1))
        raise AssertionError("unknown unit accepted")
    except waste.WasteError as e:
        assert "Satuan" in str(e)


def test_investigation_prefills_the_form_of_one_branch(monkeypatch):
    rows = [{"id": "a", "branch_code": "CCI01", "branch_name": "Kopi Calf Kemang", "status": "submitted", "investigation_id": None,
             "business_date": date(2026, 10, 30), "category": "Premix Expired/ Over Stock", "item_name": "PREMIX MATCHA",
             "qty": 500, "unit": "GR", "full_name": "Andri", "username": "andri", "note": "asam"},
            {"id": "b", "branch_code": "CCI02", "branch_name": "Kopi Calf Pademangan", "status": "submitted",
             "investigation_id": None, "business_date": date(2026, 10, 30), "category": "x", "item_name": "y", "qty": 1,
             "unit": "PCS", "full_name": None, "username": "z", "note": None}]
    monkeypatch.setattr(waste.db, "fetch", lambda q, p=None: rows)
    with pytest.raises(waste.WasteError, match="cabang yang sama"):
        waste.open_investigation(["a", "b"], {"username": "cc", "full_name": "CC"}, None)
    with pytest.raises(waste.WasteError, match="di luar cabang"):
        waste.open_investigation(["a", "b"], {"username": "cc"}, ("CCI01",))


def test_cost_control_corrects_an_entry_and_the_change_is_logged(monkeypatch):
    # 2026-11-10 (migration 087): qty, category and date fixed in place, one log row per field, a reason required
    from contextlib import contextmanager
    from decimal import Decimal
    row = _row(scope="waste", item_code="RMFR-00003", item_name="DIMSUM", unit="PCS", qty=Decimal("5.000"),
               category="Packaging Rusak", investigation_id=None)
    done = {}

    class Cur:
        def execute(self, sql, args=None):
            if sql.lstrip().startswith("UPDATE"):
                done["update"] = args
        def fetchone(self):
            return dict(row)
        def executemany(self, sql, rows):
            done["log"] = rows
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()

    @contextmanager
    def tx():
        yield Conn()

    monkeypatch.setattr(waste.db, "transaction", tx)
    monkeypatch.setattr(waste.db, "fetchrow", lambda *a, **k: {**row, "qty": Decimal("3"), "corrections": 3})
    monkeypatch.setattr(waste, "catalog", lambda *a, **k: CATALOG)
    monkeypatch.setattr(waste, "today", lambda: date(2026, 11, 10))
    monkeypatch.setattr(waste, "with_values", lambda rows: [waste._view(r) for r in rows])
    import app.running_stock as rs
    monkeypatch.setattr(rs, "clear", lambda: None)
    user = {"id": None, "username": "cc", "full_name": "Cost Control"}
    with pytest.raises(waste.WasteError):
        waste.correct(row["id"], {"qty": 3}, user, None)  # no reason
    with pytest.raises(waste.WasteError):
        waste.correct(row["id"], {"reason": "salah", "qty": 5}, user, None)  # nothing changed
    assert waste.correct(row["id"], {"reason": "salah", "qty": 3}, user, ("CCI99",)) is None  # not in the reviewer's branches
    out = waste.correct(row["id"], {"reason": "salah input", "qty": 3, "category": "Premix Expired/ Over Stock",
                                    "businessDate": date(2026, 10, 30)}, user, None)
    assert out["changed"] == ["qty", "category", "businessDate"] and out["corrections"] == 3
    assert done["update"]["qty"] == Decimal("3.000") and done["update"]["business_date"] == date(2026, 10, 30)
    assert [r[1:4] for r in done["log"]] == [("qty", "5 PCS", "3 PCS"), ("category", "Packaging Rusak", "Premix Expired/ Over Stock"),
                                             ("businessDate", "2026-10-31", "2026-10-30")]


def test_a_new_entry_returns_no_correction_subquery():
    # 2026-11-10: the count of corrections in RETURNING compared waste_correction.id (bigint) to the entry id (uuid)
    assert "waste_correction" not in waste.COLUMNS_NEW and "0 AS corrections" in waste.COLUMNS_NEW
    assert "c.entry_id = e.id" in waste.COLUMNS


def test_monitor_filters_several_categories(monkeypatch):
    seen = []
    monkeypatch.setattr(waste.db, "fetch", lambda q, p=None: seen.append(p) or [])
    monkeypatch.setattr(waste.db, "fetchrow", lambda q, p=None: {"entries": 0})
    monkeypatch.setattr(waste, "with_values", lambda rows: [])
    waste.monitor(date(2026, 11, 1), date(2026, 11, 12), None, "", "Expired|Tumpah| ")
    assert seen[0]["cat"] == ["Expired", "Tumpah"]


def test_bulk_category_corrects_each_entry(anon, store, page_access, monkeypatch):
    calls = []

    def correct(entry_id, body, user, branches):
        calls.append((entry_id, body["category"]))
        if entry_id == "same":
            raise waste.WasteError("Tidak ada perubahan")
        return None if entry_id == "gone" else {"id": entry_id}
    monkeypatch.setattr(waste, "correct", correct)
    login(anon, "kasir", "Kasir1234")
    page_access.overrides[str(store.user["id"])] = {"cc.waste-review": True}
    r = anon.post("/api/waste/reviews/category", json={"ids": ["a", "b", "same", "gone", "a"], "category": "Expired", "reason": "salah pilih"})
    assert r.status_code == 200 and r.json()["updated"] == 2 and len(r.json()["skipped"]) == 2
    assert [c[0] for c in calls] == ["a", "b", "same", "gone"]


def test_recipe_line_of_counted_item_keeps_its_base_unit():
    """2026-11-12: KALENG 330ML + STICKER (premix of two PCS items) had no value: the recipe quantity was read as grams."""
    from app import waste
    units = {"PCPB-00008": ("PCS", {"PACK@150PCS": 150.0, "PCS": 1.0}), "MILK": ("GR", {})}
    r = {"scope": "premix", "item_code": "KALENG-330ML-STICKER", "qty": 2, "unit": "SET",
         "exploded": [{"code": "PCPB-00008", "grams": 2.0, "isStock": True}, {"code": "MILK", "grams": 30.0, "isStock": True},
                      {"code": "WATER", "grams": 5.0, "isStock": False}]}
    parts = waste.entry_parts(r)
    assert [p[0] for p in parts] == ["PCPB-00008", "MILK"]
    assert [waste.base_qty(units, c, q, u, rec) for c, q, u, rec in parts] == [2.0, 30.0]
    # an item entered by itself in a unit that cannot be converted stays without a quantity
    assert waste.base_qty(units, "PCPB-00008", 1, "KG", False) is None
