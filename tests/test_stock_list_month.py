from datetime import date

from app.stock_list_month import figures, month_bounds


def test_month_bounds():
    assert month_bounds("2026-11") == (date(2026, 11, 1), date(2026, 11, 30))
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


def test_estimate_actual_variance():
    f = figures(prev=100, moves={"received": 50, "transferIn": 10, "transferOut": -5, "outletBuy": 2}, transit_in=3, transit_out=1,
                sales=60, non_sales=4, journal=6, product=2, ending=70)
    # Item Journal = waste without Product Sales; other = outlet purchase (no column, inside the estimate)
    assert (f["journal"], f["productSales"], f["other"], f["transferOut"]) == (4, 2, 2, 5)
    before_use = 100 + 50 + 10 + 3 + 2 - 5 - 1 - 4 - 2
    assert f["system"] == 64
    assert f["estimate"] == before_use - 64
    assert f["actual"] == before_use - 70
    assert f["variance"] == f["actual"] - f["system"] == f["estimate"] - 70
    assert abs(f["variancePct"] - f["variance"] / 64) < 1e-6


def test_no_checkpoint_yet():
    f = figures(prev=10, moves={}, transit_in=0, transit_out=0, sales=4, non_sales=0, journal=0, product=0, ending=None)
    assert f["estimate"] == 6 and f["ending"] is None and f["actual"] is None and f["variance"] is None and f["variancePct"] is None


def test_no_system_usage_no_pct():
    f = figures(prev=10, moves={}, transit_in=0, transit_out=0, sales=0, non_sales=0, journal=0, product=0, ending=8)
    assert f["variance"] == 2 and f["variancePct"] is None
