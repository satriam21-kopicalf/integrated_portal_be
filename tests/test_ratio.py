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
    # without an MDR the ratios use the ESB nett sales
    plain = ratio._row("CTG54", "x", 506209000.0, 467769108.0, 1, 0.0, {"bb": 1.0}, 0.0, so_a, {"value": None}, None)
    assert plain["netBasis"] == "nett" and plain["pendapatan"] is None and plain["usage"] is None


def test_ratio_page_needs_its_page(anon, store, page_access, monkeypatch):
    monkeypatch.setattr(ratio, "ratio", lambda start, end, codes=None: {"outlets": [], "total": {}, "codes": codes})
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/cost-control/ratio").status_code == 403
    page_access.overrides[str(store.user["id"])] = {"cc.ratio": True}
    r = anon.get("/api/cost-control/ratio?branch=TGP17,CCI01")
    assert r.status_code == 200 and r.json()["codes"] == ["CCI01"]  # role user: its own branches only
    assert anon.get("/api/cost-control/summary").status_code == 403  # COGS & usage is another page
    login(anon, "superadmin", "Admin1234")
    assert anon.get("/api/cost-control/ratio?dateFrom=2026-01-01&dateTo=2026-09-30").status_code == 400  # > 3 months
