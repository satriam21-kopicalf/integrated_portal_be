"""Outlet ratio (app/ratio.py): the formulas of the Cost Control workbook "CALF - Rasio Outlet", checked on the
figures of Kopi Calf Cempaka Putih in sheet SEP-W5 (1-30 Sep 2026)."""
import pytest

from app import ratio
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)


def test_formulas_match_the_workbook():
    so_a = {"value": 30900608.0}
    so_b = {"value": 50593894.0}
    row = ratio._row("CTG54", "Kopi Calf Cempaka Putih", gross=506209000.0, nett=467769108.0, bills=1, non_sales=6172000.0,
                     buy={"bb": 172963389.0, "espresso": 77775000.0, "dimsum": 10998760.0}, journal=1385821.4,
                     so_a=so_a, so_b=so_b, mdr_row={"pct": 23.99105172})
    # workbook values (SEP-W5, row Kopi Calf Cempaka Putih)
    assert row["cogs"] == 261737149
    assert row["pendapatan"] == pytest.approx(384764137, abs=1)
    assert row["espressoShare"] == pytest.approx(0.2971492595, abs=1e-6)
    assert row["bbRatio"] == pytest.approx(0.4781166728, abs=1e-6)
    assert row["espressoRatio"] == pytest.approx(0.2021368223, abs=1e-6)
    assert row["cogsRatioGross"] == pytest.approx(0.5170535273, abs=1e-6)
    assert row["cogsRatioNet"] == pytest.approx(0.6802534951, abs=1e-6)
    assert row["usageRatioGross"] == pytest.approx(0.4781500586, abs=1e-6)
    assert row["usageRatioNet"] == pytest.approx(0.6290707468, abs=1e-6)
    assert row["delta"] == pytest.approx(0.05118274835, abs=1e-6)
    # the gross view: every ratio on gross sales
    assert row["net"] == row["pendapatan"]
    assert row["bbRatioGross"] == pytest.approx((172963389.0 + 10998760.0) / 506209000.0, abs=1e-6)
    assert row["espressoRatioGross"] == pytest.approx(77775000.0 / 506209000.0, abs=1e-6)
    assert row["deltaGross"] == pytest.approx(0.5170535273 - 0.4781500586, abs=1e-6)
    total = ratio._total([row])
    assert total["bbRatioGross"] == pytest.approx(row["bbRatioGross"]) and total["nonSalesPctNet"] == pytest.approx(6172000.0 / row["pendapatan"])
    # without an MDR the ratios use the ESB nett sales
    plain = ratio._row("CTG54", "x", 506209000.0, 467769108.0, 1, 0.0, {"bb": 1.0}, 0.0, so_a, {"value": None}, None)
    assert plain["netBasis"] == "nett" and plain["pendapatan"] is None and plain["usage"] is None


def test_ratio_page_needs_its_page(anon, store, page_access, monkeypatch):
    monkeypatch.setattr(ratio, "ratio", lambda start, end, codes=None, purchases="invoice": {"outlets": [], "total": {}, "codes": codes})
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/cost-control/ratio").status_code == 403
    page_access.overrides[str(store.user["id"])] = {"cc.ratio": True}
    r = anon.get("/api/cost-control/ratio?branch=TGP17,CCI01")
    assert r.status_code == 200 and r.json()["codes"] == ["CCI01"]  # role user: its own branches only
    login(anon, "superadmin", "Admin1234")
    assert anon.get("/api/cost-control/ratio?dateFrom=2026-01-01&dateTo=2026-09-30").status_code == 400  # > 3 months


def test_purchases_basis_gr_groups_by_category_and_counts_the_transfers(monkeypatch):
    """2026-11-13: basis "gr" — the goods that came in, by the cost group of their ESB category; General Supplies apart
    from the COGS; Transfer Stock in − out in the usage. A category not set follows its supplier as the workbook."""
    mapping = {"PACKAGING": "gs", "SUSU": "bb"}
    assert ratio.group_of("PACKAGING", "Calf Warehouse", "goods_receipt", mapping) == "gs"
    assert ratio.group_of("KOPI", "Calf Central Kitchen", "goods_receipt", mapping) == "espresso"  # not set: its supplier
    assert ratio.group_of("BUAH", None, "simple_purchase", mapping) == "bb"  # an outlet purchase
    assert ratio.group_of("X", "Toko Lain", "goods_receipt", mapping) == "other"
    row = ratio._row("A", "Outlet A", 1000.0, 900.0, 1, 0.0, {"bb": 300.0, "espresso": 100.0, "gs": 50.0, "transfer": -40.0}, 0.0,
                     {"value": 200.0}, {"value": 150.0}, None)
    assert row["cogs"] == 400 and row["gs"] == 50 and row["gsRatio"] == pytest.approx(50 / 900, abs=1e-6)
    assert row["usage"] == 400 - 40 + 200 - 150  # the goods sent to another outlet were not used here
    rows = [{"to_branch": "A", "from_branch": "B", "value": 100}, {"to_branch": "B", "from_branch": "A", "value": 30}]
    monkeypatch.setattr(ratio.db, "fetch", lambda q, p=None: rows)
    assert ratio.transfers(None, None, None) == {"A": 70.0, "B": -70.0}
    assert ratio.transfers(None, None, ["A"]) == {"A": 70.0}


def test_a_cost_group_change_applies_from_its_date_on(monkeypatch):
    """2026-11-13: the group of a category has a valid-from date; the months before keep theirs; no change of a closed month."""
    from datetime import date
    vers = {"PACKAGING": [(date(2026, 11, 1), "bb"), (date(2026, 12, 1), "gs")], "ESPRESSO": [(date(2026, 11, 1), "espresso"), (date(2026, 11, 20), None)]}
    assert ratio.group_on(vers, "PACKAGING", date(2026, 10, 31)) is None  # before any version: its supplier
    assert ratio.group_on(vers, "PACKAGING", date(2026, 11, 30)) == "bb"
    assert ratio.group_on(vers, "PACKAGING", date(2026, 12, 1)) == "gs"
    assert ratio.mapping_on(vers, date(2026, 11, 25)) == {"PACKAGING": "bb"}  # ESPRESSO back to its supplier from the 20th
    rows = [{"branch_code": "A", "module": "goods_receipt", "supplier": "Calf Warehouse", "category": "PACKAGING", "day": date(2026, 11, 30), "value": 100, "unpriced": 0},
            {"branch_code": "A", "module": "goods_receipt", "supplier": "Calf Warehouse", "category": "PACKAGING", "day": date(2026, 12, 2), "value": 40, "unpriced": 1}]
    monkeypatch.setattr(ratio, "versions", lambda: vers)
    monkeypatch.setattr(ratio.db, "fetch", lambda q, p=None: rows)
    buy, unpriced = ratio.gr_purchases(date(2026, 11, 1), date(2026, 12, 31), None)
    assert buy == {"A": {"bb": 100.0, "gs": 40.0}} and unpriced == {"A": 1}
    import app.utils as u
    monkeypatch.setattr(u, "today", lambda: date(2026, 11, 13))
    with pytest.raises(ValueError):
        ratio.set_cost_group("PACKAGING", "gs", date(2026, 10, 31), "x", "u")  # a closed month
    with pytest.raises(ValueError):
        ratio.set_cost_group("PACKAGING", "gs", date(2026, 12, 1), "  ", "u")  # no reason
