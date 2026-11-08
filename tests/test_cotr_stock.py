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
