"""COTR storage & production stock (migration 081): count + received − used by the batches − waste."""
from datetime import date

from app import cotr


def test_running_stock_of_a_hub(monkeypatch):
    items = [{"code": "MILK", "name": "Milk", "category": "raw", "unit": "ml", "pack_unit": "Box", "pack_qty": 1000, "cost": 20, "is_stock": True, "is_active": True}]

    def fetch(q, p=None):
        if "FROM integration_portal.cotr_item" in q: return items
        if "cotr_stock_count_line" in q: return [{"item_code": "MILK", "qty": 5000}]
        if "cotr_receipt" in q: return [{"item_code": "MILK", "qty": 2000}]
        if "cotr_bom" in q: return [{"code": "B-LATTE", "output": "LATTE", "yield_qty": 1, "lines": [{"item": "MILK", "qty": 150, "unit": "ml"}]}]
        if "cotr_menu" in q: return [{"code": "LATTE", "bom_code": "B-LATTE"}]
        if "cotr_batch" in q: return [{"menu_code": "LATTE", "qty": 20}]
        if "cotr_waste" in q: return [{"code": "MILK", "qty": 1, "unit": "Box", "kind": "production"}]
        return []
    monkeypatch.setattr(cotr.db, "fetch", fetch)
    monkeypatch.setattr(cotr.db, "fetchrow", lambda q, p=None: {"id": "c1", "day": date(2026, 11, 1)})
    out = cotr.stock("HUB", date(2026, 11, 8))
    r = out["rows"][0]
    # 5000 counted + 2000 received − 20 cups × 150 ml − 1 box (1000 ml) wasted = 3000 ml
    assert (r["so"], r["received"], r["used"], r["waste"], r["estimate"]) == (5000, 2000, 3000, 1000, 3000)
    assert r["value"] == 60000 and out["soDay"] == "2026-11-01"


def test_an_empty_text_field_is_saved_empty_not_null(monkeypatch):
    """2026-11-09: a new pool without an address was refused (address may not be null)."""
    seen = {}
    monkeypatch.setattr(cotr, "_not_null_text", lambda table: {"address", "city", "name", "code", "kind"})
    def fetchrow(q, p=None):
        seen["p"] = p
        return {"code": "P1", "name": "Pool", "address": ""}
    monkeypatch.setattr(cotr.db, "fetchrow", fetchrow)
    cotr.master_save({"id": "u"}, "pools", None, {"code": "p1", "name": "Pool", "kind": "parent", "city": "Jakarta", "address": ""})
    assert "" in seen["p"] and None not in seen["p"][:5]


def test_a_pool_cupping_of_a_menu_is_named_by_the_menu(monkeypatch):
    """migration 088: waste / cupping of a pool per menu: the menu names it, in cups; a reason stays required."""
    seen = {}
    monkeypatch.setattr(cotr, "_menu_map", lambda: {"LATTE": {"code": "LATTE", "name": "Es Kopi Latte"}})
    def fetchrow(q, p=None):
        seen["p"] = p
        return {"id": "w1", "item": p[2], "unit": p[4], "kind": p[7], "menu_code": p[10]}
    monkeypatch.setattr(cotr.db, "fetchrow", fetchrow)
    out = cotr.waste_add({"id": "u"}, {"pool_code": "POOL", "kind": "cupping", "menu_code": "LATTE", "qty": 4, "reason": "tasting CFD"})
    assert (out["item"], out["unit"], out["kind"], out["menu_code"]) == ("Es Kopi Latte", "cup", "cupping", "LATTE")
    assert seen["p"][9] is None  # not an item of the stock
    import pytest
    with pytest.raises(cotr.CotrError):
        cotr.waste_add({"id": "u"}, {"pool_code": "POOL", "kind": "cupping", "menu_code": "LATTE", "qty": 4, "reason": " "})


def test_an_area_request_needs_an_area_or_a_located_new_place(monkeypatch):
    import pytest
    monkeypatch.setattr(cotr, "require_driver", lambda u: {"code": "D1", "pool_code": "POOL"})
    with pytest.raises(cotr.CotrError, match="Pilih area"):
        cotr.send_area_request({"id": "u"}, {})
    with pytest.raises(cotr.CotrError, match="lokasi"):
        cotr.send_area_request({"id": "u"}, {"name": "Taman Baru"})
    with pytest.raises(cotr.CotrError, match="Jam selesai"):
        cotr.send_area_request({"id": "u"}, {"area": "A1", "start": "10:00", "end": "09:00"})
    with pytest.raises(cotr.CotrError, match="Jam tidak valid"):
        cotr.send_area_request({"id": "u"}, {"area": "A1", "start": "25:00"})


def test_a_rejected_area_request_needs_a_reason():
    import pytest
    with pytest.raises(cotr.CotrError, match="alasan"):
        cotr.decide_area_request({"id": "u"}, "x", False, {})


def test_a_fleet_issue_moves_by_steps_and_closing_needs_a_note(monkeypatch):
    import pytest
    class Conn:
        def execute(self, q, p=None):
            class R:
                def fetchone(_s): return {"id": "i1", "status": "open", "handler": None, "cost": None}
            return R()
    class Tx:
        def __enter__(self): return Conn()
        def __exit__(self, *a): return False
    monkeypatch.setattr(cotr.db, "transaction", lambda: Tx())
    with pytest.raises(cotr.CotrError, match="Tulis apa"):
        cotr.handle_issue({"id": "u"}, "i1", {"status": "resolved"})
    with pytest.raises(cotr.CotrError, match="tidak dapat"):
        cotr.handle_issue({"id": "u"}, "i1", {"status": "open"})
    monkeypatch.setattr(cotr, "require_driver", lambda u: {"code": "D1", "fleet_code": None})
    with pytest.raises(cotr.CotrError, match="armada"):
        cotr.report_issue({"id": "u"}, {"category": "brake", "severity": "high", "title": "Rem blong"})


def test_a_pay_deduction_needs_its_note_and_a_published_month_is_locked(monkeypatch):
    """migration 092: kasbon / minus / warning need a note; a published month cannot change."""
    import pytest
    from datetime import date as d
    monkeypatch.setattr(cotr, "is_published", lambda m: False)
    with pytest.raises(cotr.CotrError, match="keterangan"):
        cotr.add_pay_adjustment({"id": "u"}, "D1", d(2026, 11, 1), "kasbon", 50000, " ")
    with pytest.raises(cotr.CotrError, match="lebih dari 0"):
        cotr.add_pay_adjustment({"id": "u"}, "D1", d(2026, 11, 1), "bonus", 0, None)
    monkeypatch.setattr(cotr, "is_published", lambda m: True)
    with pytest.raises(cotr.CotrError, match="diterbitkan"):
        cotr.add_pay_adjustment({"id": "u"}, "D1", d(2026, 11, 1), "bonus", 100000, None)


def test_reconcile_sheet_against_esb(monkeypatch):
    from datetime import date
    from app import cotr_esb, cotr_ledger
    d1, d2 = date(2026, 11, 1), date(2026, 11, 2)
    monkeypatch.setattr(cotr_ledger, "_menu_map", lambda: {"M1": {"code": "M1", "name": "Premium", "sort": 1}})

    def fetch(q, p=None):
        if "FROM integration_portal.cotr_pool\n" in q or q.strip().startswith("SELECT code, name"):
            return [{"code": "HUB-A", "name": "Hub A"}]
        if "cotr_pool_move" in q:
            return [{"pool_code": "HUB-A", "day": d1, "menu_code": "M1", "kind": "sold", "qty": 10},
                    {"pool_code": "HUB-A", "day": d2, "menu_code": "M1", "kind": "sold", "qty": 5}]
        return []
    monkeypatch.setattr(cotr_ledger.db, "fetch", fetch)
    monkeypatch.setattr(cotr_ledger, "_system", lambda *a: {})
    monkeypatch.setattr(cotr_esb, "figures", lambda a, b: {"HUB-A": {d1: {"M1": {"sold": 12}}}})
    r = cotr_ledger.reconcile(None, d1, d2)
    assert r["summary"]["diff"] == 1 and r["summary"]["sheet"] == 1
    first = r["days"][0]
    assert first["status"] == "diff" and first["gap"] == 2
    assert first["lines"][0] | {} == {"menu": "M1", "name": "Premium", "kind": "sold", "sheet": 10, "esb": 12, "app": None, "diff": -2}
    assert r["days"][1]["status"] == "sheet" and r["days"][1]["lines"][0]["esb"] is None


def test_label_alias_wins_and_ignored():
    from app import cotr_ledger
    drivers = [("bakri", "bakrisaputra", "D1"), ("dahlan", "dahlan", "D2")]
    assert cotr_ledger._label_key("BRI 55 BAKRI") == "bakri"
    assert cotr_ledger._match("BRI 55 BAKRI", drivers) == "D1"
    assert cotr_ledger._match("BRI 55 BAKRI", drivers, {"bakri": "D2"}) == "D2"
    assert cotr_ledger._match("BNI 47 PESENAN POOL", drivers, {"pesenanpool": None}) is None


def test_dashboard_adds_up_the_pools(monkeypatch):
    from datetime import date
    from app import cotr_dashboard, cotr_ledger
    day = date(2026, 11, 11)
    monkeypatch.setattr(cotr_dashboard.db, "fetch", lambda q, p=None: [{"code": "HUB-A", "name": "Hub A"}] if "cotr_pool" in q and "menu" not in q
                        else [{"code": "M1", "name": "Premium", "price": 10000}] if "cotr_menu" in q else [])
    monkeypatch.setattr(cotr_dashboard.db, "fetchrow", lambda q, p=None: {"n": 2})
    monkeypatch.setattr(cotr_ledger, "run", lambda code, a, b: {"days": [
        {"day": "2026-11-11", "source": "sheet", "rows": {"M1": {"sold": 5, "waste": 1, "cupping": 0, "arrived": 0, "transfer": 0, "actual": 7, "balance": 1, "expired": 0}}}]})
    monkeypatch.setattr(cotr_ledger, "report_armada", lambda *a: {"drivers": [{"code": "D1", "name": "Budi", "pool": "HUB-A",
                        "days": [{"status": "sheet", "total": 50000, "lines": {"M1": {"t": 5}}}]}]})
    monkeypatch.setattr(cotr_ledger, "reconcile", lambda *a: {"summary": {"diff": 3}})
    monkeypatch.setattr(cotr_ledger, "unmatched_labels", lambda p: {"unmatched": [1]})
    r = cotr_dashboard.dashboard(day)
    assert r["summary"]["sold"] == 5 and r["summary"]["revenue"] == 50000 and r["summary"]["balance"] == 1
    assert r["drivers"][0]["cups"] == 5 and r["todo"]["reconcileDiff"] == 3 and r["todo"]["unmatchedNames"] == 1


def test_close_day_needs_count_and_note(monkeypatch):
    from datetime import date
    import pytest as _pt
    from app import cotr_ledger
    from app.cotr import CotrError
    state = {"openShifts": [], "counted": False, "balance": 0, "menus": [], "waste": {}}
    monkeypatch.setattr(cotr_ledger, "closing", lambda p, d: dict(state))
    with _pt.raises(CotrError, match="Sisa Aktual"):
        cotr_ledger.close_day({"id": None}, "HUB-A", date(2026, 11, 11), None)
    state.update(counted=True, balance=-2)
    with _pt.raises(CotrError, match="penjelasan"):
        cotr_ledger.close_day({"id": None}, "HUB-A", date(2026, 11, 11), " ")
    state.update(openShifts=[{"code": "D1", "name": "Budi"}])
    with _pt.raises(CotrError, match="belum check out"):
        cotr_ledger.close_day({"id": None}, "HUB-A", date(2026, 11, 11), "x")


def test_fifo_gives_the_oldest_lots_first(monkeypatch):
    from datetime import date
    from app import cotr_ledger
    day = date(2026, 11, 12)
    monkeypatch.setattr(cotr_ledger, "_menu_map", lambda: {"M1": {"code": "M1", "name": "Premium", "shelf_life_days": 5}})
    monkeypatch.setattr(cotr_ledger, "run", lambda *a: {"days": [{"rows": {"M1": {"arrived": 10, "transfer": 0}}}]})

    def fetch(q, p=None):
        if "FROM integration_portal.cotr_batch" in q:
            return []
        if "cotr_pool_count" in q:
            return [{"menu_code": "M1", "batch_no": "0210", "qty": 4, "expires_on": date(2026, 11, 15)},
                    {"menu_code": "M1", "batch_no": "0108", "qty": 3, "expires_on": date(2026, 11, 13)},
                    {"menu_code": "M1", "batch_no": "0105", "qty": 2, "expires_on": date(2026, 11, 10)}]
        if "cotr_dispatch" in q:
            return [{"driver_code": "D1", "name": "A", "status": "assigned", "lines": {"M1": 5}, "final_lines": None, "assigned_at": None},
                    {"driver_code": "D2", "name": "B", "status": "assigned", "lines": {"M1": 15}, "final_lines": None, "assigned_at": None}]
        return []
    monkeypatch.setattr(cotr_ledger.db, "fetch", fetch)
    monkeypatch.setattr(cotr_ledger.db, "fetchrow", lambda q, p=None: {"day": date(2026, 11, 11)})
    r = cotr_ledger.fifo("HUB-A", day)
    m = r["menus"][0]
    assert [l["batchNo"] for l in m["expired"]] == ["0105"]
    assert [(l["batchNo"], l["qty"]) for l in r["drivers"][0]["lines"]] == [("0108", 3), ("0210", 2)]
    b = r["drivers"][1]["lines"]
    assert [(l["batchNo"], l["qty"], l.get("short", False)) for l in b] == [("0210", 2, False), (None, 10, False), (None, 3, True)]


def test_shipment_plan_needs_less_what_is_good(monkeypatch):
    from datetime import date
    from app import cotr_ledger
    day = date(2026, 11, 12)
    monkeypatch.setattr(cotr_ledger, "_menu_map", lambda: {"M1": {"code": "M1", "name": "Premium", "sort": 1, "shelf_life_days": 3}})
    monkeypatch.setattr(cotr_ledger, "run", lambda *a: {"days": [
        {"source": "sheet", "rows": {"M1": {"sold": 100, "waste": 4, "cupping": 6}}},
        {"source": "sheet", "rows": {"M1": {"sold": 80, "waste": 0, "cupping": 10}}}]})

    def fetch(q, p=None):
        if "DISTINCT pool_code" in q:
            return [{"code": "HUB-A"}]
        if "SELECT code, name" in q:
            return [{"code": "HUB-A", "name": "Hub A"}]
        if "cotr_pool_count" in q:
            return [{"menu_code": "M1", "batch_no": "0110", "qty": 30, "expires_on": date(2026, 11, 13)},
                    {"menu_code": "M1", "batch_no": "0108", "qty": 5, "expires_on": date(2026, 11, 11)}]
        return []
    monkeypatch.setattr(cotr_ledger.db, "fetch", fetch)
    monkeypatch.setattr(cotr_ledger.db, "fetchrow", lambda q, p=None: {"day": date(2026, 11, 11)})
    r = cotr_ledger.shipment_plan(day, 7, 10)
    row = r["pools"][0]["rows"][0]
    # (90 sold + 10 used) × 1.1 = 110 needed; 30 good (5 expired) → send 80
    assert (row["need"], row["have"], row["expiring"], row["send"]) == (110, 30, 5, 80)
    assert r["totals"] == [{"menu": "M1", "name": "Premium", "send": 80}]
