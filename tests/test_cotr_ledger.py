"""Product stock of a pool per day (migration 089), as the sheet "Product": the physical leftover starts the next day."""
from datetime import date

from app import cotr_ledger as L

MENUS = {"PRM": {"code": "PRM", "name": "Premium", "sort": 1, "shelf_life_days": 3, "is_active": True, "price": 15000}}


def _setup(monkeypatch, counts, moves=(), sales=()):
    monkeypatch.setattr(L, "_menu_map", lambda: MENUS)
    monkeypatch.setattr(L, "first_count", lambda pool: min(c["day"] for c in counts))

    def fetch(q, p=None):
        if "cotr_pool_count" in q: return [c for c in counts]
        if "cotr_pool_move" in q: return list(moves)
        if "cotr_sale" in q: return list(sales)
        return []
    monkeypatch.setattr(L.db, "fetch", fetch)


def test_the_sheet_product_of_a_day(monkeypatch):
    counts = [{"day": date(2026, 11, 7), "menu_code": "PRM", "batch_no": "0107", "qty": 100, "expires_on": None},
              {"day": date(2026, 11, 7), "menu_code": "PRM", "batch_no": "0104", "qty": 5, "expires_on": None},  # made 4 Nov, expires 7 Nov
              {"day": date(2026, 11, 8), "menu_code": "PRM", "batch_no": "0108", "qty": 118, "expires_on": None}]
    moves = [{"day": date(2026, 11, 8), "menu_code": "PRM", "kind": k, "source": "sheet", "qty": n}
             for k, n in (("arrived", 120), ("sold", 95), ("waste", 2), ("cupping", 3))]
    _setup(monkeypatch, counts, moves)
    out = L.run("POOL", date(2026, 11, 8), date(2026, 11, 8))
    r = out["days"][0]["rows"]["PRM"]
    # opening 105 − 5 expired = 100; 100 + 120 − 95 − 2 − 3 = 120 calculated; 118 counted = −2
    assert (r["opening"], r["calculated"], r["actual"], r["balance"]) == (100, 120, 118, -2)
    assert r["lots"][0]["expiresOn"] == "2026-11-11" and out["days"][0]["source"] == "sheet"


def test_a_batch_code_is_the_sequence_and_the_day():
    assert L.batch_day("0210", date(2026, 11, 11)) == date(2026, 11, 10)
    assert L.batch_day("0128", date(2026, 11, 2)) == date(2026, 10, 28)
    assert L.batch_day("x", date(2026, 11, 2)) is None


def test_the_names_of_the_report_armada_find_their_driver():
    drivers = [("dahlan", "dahlanputra", "D1"), ("bahanuali", "bahanualinuhugraha", "D2"), ("", "muhammadaprizal", "D3"), ("adit", "aditfirmansyah", "D4"),
               ("adit", "aditya", "D5")]
    assert L._match("BNI 10 DAHLAN", drivers) == "D1"
    assert L._match("BNI 147 BAHANUALI", drivers) == "D2"
    assert L._match("BNI 37 M.APRIZAL", drivers) == "D3"
    assert L._match("BNI 91 ADIT", drivers) is None  # two Adit: not guessed
