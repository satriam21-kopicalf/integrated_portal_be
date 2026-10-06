"""Cost Control: pure helpers (periods, status bands, ratios) and the settings guard."""
from datetime import date

from app.cost_control import period_bounds, periods_between
from app.routes import cost_control as cc

SETTINGS = {k: dict(v) for k, v in cc.DEFAULT_SETTINGS.items()}


def row(**kw):
    base = {c: 0 for c in cc.SUM_COLS}
    base.update(begin_value=0, end_value=0, opname_count=0, pending_opname_count=0, last_opname_date=None)
    base.update(kw)
    return base


def test_period_bounds_follow_the_opname_rhythm():
    assert period_bounds(date(2026, 9, 1)) == (date(2026, 9, 1), date(2026, 9, 7))
    assert period_bounds(date(2026, 9, 14)) == (date(2026, 9, 8), date(2026, 9, 14))
    assert period_bounds(date(2026, 9, 15)) == (date(2026, 9, 15), date(2026, 9, 21))
    assert period_bounds(date(2026, 9, 30)) == (date(2026, 9, 22), date(2026, 9, 30))
    assert period_bounds(date(2026, 2, 25)) == (date(2026, 2, 22), date(2026, 2, 28))
    assert period_bounds(date(2028, 2, 29)) == (date(2028, 2, 22), date(2028, 2, 29))


def test_periods_between_covers_whole_months():
    ps = periods_between(date(2026, 8, 20), date(2026, 9, 9))
    assert ps == [
        (date(2026, 8, 15), date(2026, 8, 21)), (date(2026, 8, 22), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 7)), (date(2026, 9, 8), date(2026, 9, 14)),
    ]


def test_band_thresholds_are_inclusive():
    b = {"good": 35, "warning": 40, "serious": 45}
    assert [cc.band(v, b) for v in (20, 35, 35.01, 40, 44.9, 45, 45.5)] == \
        ["good", "good", "warning", "warning", "serious", "serious", "critical"]
    assert cc.band(None, b) is None


def test_metrics_ratios_on_both_bases():
    m = cc.metrics(row(net_sales=1_000_000, subtotal=1_100_000, theoretical_cogs=350_000, other_usage=10_000,
                       posted_variance=-30_000, actual_cogs=390_000, opname_count=1), SETTINGS)
    assert m["actualPctNet"] == 39.0 and m["theoreticalPctNet"] == 35.0
    assert m["actualPctSubtotal"] == round(390_000 / 1_100_000 * 100, 2)
    assert m["gapPpNet"] == 4.0
    assert m["usageRatio"] == round(390_000 / 350_000 * 100, 2)
    assert m["variance"] == -30_000
    assert m["status"]["cogsNet"] == "warning"           # 39% of net sales
    assert m["status"]["gapNet"] == "critical"           # 4 pp > 3
    assert m["status"]["waste"] == "good"                # 1%


def test_usage_status_needs_an_opname():
    m = cc.metrics(row(net_sales=1_000_000, subtotal=1_000_000, theoretical_cogs=300_000, actual_cogs=300_000), SETTINGS)
    assert m["hasOpname"] is False
    assert m["status"]["usage"] is None and m["status"]["gapNet"] is None
    assert m["status"]["cogsNet"] == "good"


def test_metrics_without_sales():
    m = cc.metrics(row(theoretical_cogs=1000, actual_cogs=1000), SETTINGS)
    assert m["actualPctNet"] is None and m["status"]["cogsNet"] is None and m["gapPpNet"] is None


def test_settings_reject_unordered_bands(client):
    r = client.put("/api/cost-control/settings", json={"cogs_bands": {"good": 50, "warning": 40, "serious": 45}})
    assert r.status_code == 422
    assert r.json()["field"] == "cogs_bands"


def test_settings_reject_unknown_keys(client):
    r = client.put("/api/cost-control/settings", json={"colour": "red"})
    assert r.status_code == 422


def test_summary_network_only_counts_locations_with_sales(client, monkeypatch):
    rows = [
        {**row(net_sales=1_000_000, subtotal=1_100_000, theoretical_cogs=300_000, actual_cogs=320_000, bills=50, opname_count=1),
         "branch_code": "CCI01", "period_start": date(2026, 9, 1), "period_end": date(2026, 9, 7), "location_id": "1"},
        # bulk-order stock location: usage but no POS sales
        {**row(theoretical_cogs=500_000, actual_cogs=500_000),
         "branch_code": "BULK1", "period_start": date(2026, 9, 1), "period_end": date(2026, 9, 7), "location_id": "9"},
        # an implausible draft-opname line was left out for this outlet
        {**row(net_sales=2_000_000, subtotal=2_000_000, theoretical_cogs=700_000, actual_cogs=720_000,
               excluded_pending_variance=-1_866_000_000, excluded_pending_lines=1, opname_count=1),
         "branch_code": "TGP14", "period_start": date(2026, 9, 1), "period_end": date(2026, 9, 7), "location_id": "5"},
    ]
    monkeypatch.setattr(cc, "period_rows", lambda start, end, branch=None: rows)
    monkeypatch.setattr(cc, "branch_names", lambda: {"BULK1": "BULK ORDER JABODETABEK"})
    monkeypatch.setattr(cc, "settings", lambda: SETTINGS)
    monkeypatch.setattr(cc, "freshness", lambda: {"refreshedAt": "x"})
    cc._cache._data.clear()
    body = client.get("/api/cost-control/summary?dateFrom=2026-09-01&dateTo=2026-09-07").json()
    t = body["total"]
    assert t["actualCogs"] == 1_040_000 and t["netSales"] == 3_000_000  # BULK1 not in the network total
    assert t["actualPctNet"] == round(1_040_000 / 3_000_000 * 100, 2)
    assert t["excludedPendingVariance"] == -1_866_000_000 and t["excludedPendingLines"] == 1
    assert body["withoutSales"] == [{"branchCode": "BULK1", "branchName": "BULK ORDER JABODETABEK",
                                     "actualCogs": 500_000, "theoreticalCogs": 500_000}]
    assert "BULK1" not in body["statusCounts"]
