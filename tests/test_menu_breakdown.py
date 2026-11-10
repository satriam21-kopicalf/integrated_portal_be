"""Sales Calculator → Breakdown (app/menu_breakdown.py, 2026-11-10)."""
import io

from openpyxl import load_workbook

from app import menu_breakdown as mb


def test_sugar_levels_of_the_choices():
    assert mb.sugar_level("Less Sugar") == "Less Sugar" and mb.sugar_level("LESS SUGAR CE") == "Less Sugar"
    assert mb.sugar_level("Half Sugar") == "Less Sugar" and mb.sugar_level("No Sugar") == "No Sugar"
    assert mb.sugar_level("EXTRA SUGAR CA") == "Extra Sugar" and mb.sugar_level("Split Sugar") == "Normal Sugar"
    assert mb.sugar_level("Extra Shoot Espresso Premium") is None and mb.sugar_level("Cup Frosted") is None


def test_the_excel_has_the_sales_transactions_layout(monkeypatch):
    row = {"month": "2026-10", "branch": "CTG54", "company": "CALF", "category": "BEVERAGE", "sub": "ORIGINAL KOPI SUSU",
           "menuId": "CALF:1", "menu": "Es Kopi Calf Premium", "card": "Es Kopi Calf Premium", "variant": "Less Sugar",
           "portions": 30.0, "code": "RMBV-00025", "material": "GULA AREN 1300GR", "unit": "GR", "qty": 300.0, "price": 30.0, "value": 9000.0}
    monkeypatch.setattr(mb, "rows", lambda *a, **k: {"rows": [row], "names": {"CTG54": "Kopi Calf Cempaka Putih"}})
    wb = load_workbook(io.BytesIO(mb.workbook(mb.date(2026, 8, 11), mb.date(2026, 11, 9), None, True, {"username": "cc"})))
    assert wb.sheetnames == ["Summary", "Data", "By Material", "By Menu"]
    data = wb["Data"]
    assert data["A1"].value == "Raw Material Breakdown per Menu" and data["A1"].font.b
    header = [c.value for c in data[11]]
    assert header[:3] == ["Month", "Branch Code", "Branch"] and data.freeze_panes == "A12"
    assert [c.value for c in data[12]][2] == "Kopi Calf Cempaka Putih" and [c.value for c in data[12]][-1] == 9000
