"""Recipe cards from Excel (app/recipe_import.py): the header is found, cards are grouped, codes are checked."""
from io import BytesIO

from openpyxl import Workbook

from app import recipe_import as ri


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["Kartu Resep"])  # a title row above the header
    ws.append(["Kode Kartu", "Nama Kartu", "Kategori", "Aktif", "Catatan", "Jenis Baris", "Kode Baris", "Nama Baris", "Qty", "Satuan", "Harga"])
    for r in rows:
        ws.append(r)
    b = BytesIO()
    wb.save(b)
    return b.getvalue()


def _fake(monkeypatch, existing=("OLD-1",)):
    monkeypatch.setattr(ri.mc, "items", lambda status="all": {"data": [{"code": "ITM-1", "name": "Susu UHT", "unit": "ML"}]})
    monkeypatch.setattr(ri.mc, "wips", lambda: {"WIP-1": {"name": "Premix Kopi"}})
    monkeypatch.setattr(ri.db, "fetch", lambda *a, **k: [{"code": c} for c in existing])


def test_groups_rows_into_cards_and_fills_names(monkeypatch):
    _fake(monkeypatch)
    cards = ri.plan(ri.read(_xlsx([
        ["new-1", "Es Kopi", "COFFEE", "Ya", None, "Bahan", "ITM-1", None, "150", "ML", None],
        ["NEW-1", None, None, None, None, "WIP", "WIP-1", None, 30, None, None],
        ["NEW-1", None, None, None, None, "Lain", "CUP", "Cup 16oz", 1, "PCS", "1.200,5"],
        ["OLD-1", "Lama", None, "Tidak", None, "Menu", "NEW-1", None, 2, None, None],
    ])))
    new = next(c for c in cards if c["code"] == "NEW-1")
    assert new["action"] == "create" and not new["errors"] and new["isActive"]
    assert [ln["type"] for ln in new["lines"]] == ["item", "wip", "other"]
    assert new["lines"][0]["name"] == "Susu UHT" and new["lines"][1]["name"] == "Premix Kopi" and new["lines"][2]["price"] == 1200.5
    old = next(c for c in cards if c["code"] == "OLD-1")
    assert old["action"] == "update" and not old["isActive"] and old["lines"][0]["type"] == "menu"


def test_reports_unknown_codes_and_bad_qty(monkeypatch):
    _fake(monkeypatch)
    cards = ri.plan(ri.read(_xlsx([
        ["X-1", "Salah", None, None, None, "Bahan", "NOPE", None, 1, None, None],
        ["X-1", None, None, None, None, "Bahan", "ITM-1", None, 0, None, None],
        ["X-2", "Bundle", None, None, None, "Menu", "GHOST", None, 1, None, None],
    ])))
    x1 = next(c for c in cards if c["code"] == "X-1")
    assert len(x1["errors"]) == 2 and "NOPE" in x1["errors"][0]
    assert "GHOST" in next(c for c in cards if c["code"] == "X-2")["errors"][0]


def test_dry_run_saves_nothing(monkeypatch):
    _fake(monkeypatch)
    called = []
    monkeypatch.setattr(ri.mc, "save_recipe", lambda *a: called.append(a))
    out = ri.run(_xlsx([["N-1", "Es", None, None, None, "Bahan", "ITM-1", None, 1, None, None]]), True, {"username": "t"})
    assert out["create"] == 1 and out["saved"] == 0 and not called
    out = ri.run(_xlsx([["N-1", "Es", None, None, None, "Bahan", "ITM-1", None, 1, None, None]]), False, {"username": "t"})
    assert out["saved"] == 1 and called


def test_a_file_without_the_columns_is_refused():
    wb = Workbook()
    wb.active.append(["apa", "saja"])
    b = BytesIO()
    wb.save(b)
    try:
        ri.read(b.getvalue())
        assert False
    except ri.ImportError_:
        pass


def test_month_window():
    from datetime import date
    from app.menu_costing import month_window
    assert month_window(date(2026, 11, 10), date(2026, 11, 18)) == (date(2026, 11, 1), date(2026, 11, 30))
    assert month_window(date(2026, 1, 31), date(2026, 2, 3)) == (date(2026, 1, 1), date(2026, 2, 28))
    assert month_window(date(2026, 12, 5), date(2026, 12, 31)) == (date(2026, 12, 1), date(2026, 12, 31))
