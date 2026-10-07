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


def test_issues_report_quantity_errors_and_stock_spikes(monkeypatch):
    import json
    from datetime import date as d

    from app import database as db

    queries = []

    def fetch(sql, params=None):
        queries.append((sql, params))
        if "SELECT DISTINCT location_id, branch_code" in sql:
            return [{"location_id": "174", "branch_code": "TGP14"}, {"location_id": "9", "branch_code": "CCI01"}]
        if "erp_document_lines l ON" in sql and "QTY" not in sql and "percentile_cont" in sql and "l.q /" in sql:
            return [{"module": "simple_manufacturing", "doc_num": "SM1 - 1", "doc_date": d(2026, 7, 15), "status_name": "Authorized",
                     "location_id": "9", "location_name": "Kopi Calf Supratman Bandung", "created_by": "CALFCKPROD2",
                     "line_type": "result", "product_code": "ESPR-00001", "product_name": "ESPRESSO PREMIUM",
                     "qty": 25163, "uom_name": "KG@1000GR", "med": 20.16, "factor": 1248.2}]
        if "inventory_valuation" in sql and "latest_end_qty" in sql:
            return [{"location_id": "9", "location_name": "Kopi Calf Supratman Bandung", "period_start": d(2026, 9, 22),
                     "period_end": d(2026, 9, 30), "product_id": "77", "product_name": "BAWANG PUTIH KUPAS", "in_qty": 250000,
                     "opname_qty": 0, "end_qty": 249880, "med": 500, "factor": 500, "latest_end_qty": 249700}]
        return []

    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(cc, "branch_names", lambda: {"CCI01": "Kopi Calf Supratman Bandung"})
    monkeypatch.setattr(cc, "period_rows", lambda *a, **k: [])
    monkeypatch.setattr(cc, "freshness", lambda: {"refreshedAt": "t1"})
    cc._cache.clear()
    cc._qty_cache.clear()
    body = json.loads(cc.get_issues(dateFrom="2026-07-01", dateTo="2026-09-30", branch="CCI01").body)
    q = body["quantityErrors"][0]
    assert q["branchCode"] == "CCI01" and q["baseQty"] == 25_163_000 and q["factor"] == 1248 and q["unit"] == "KG@1000GR"
    s = body["stockSpikes"][0]
    assert s["open"] is True and s["factor"] == 500 and s["branchCode"] == "CCI01"
    # bulk-order locations are excluded; usual qty learnt from the whole history, computed once and filtered per request
    qty_sql, qty_params = next((s, p) for s, p in queries if "l.q /" in s)
    assert "NOT ILIKE 'BULK%%'" in qty_sql and qty_params["since"] == cc.QTY_HISTORY_FROM
    assert qty_params["factor"] == cc.QTY_ERROR_FACTOR == 200
    n = len(queries)
    other = json.loads(cc.get_issues(dateFrom="2026-07-01", dateTo="2026-09-30", branch="TGP14").body)
    assert other["quantityErrors"] == [] and other["stockSpikes"] == []      # other outlet: filtered out
    assert not any("l.q /" in s for s, _ in queries[n:])                    # served from the cache
    early = json.loads(cc.get_issues(dateFrom="2026-08-01", dateTo="2026-08-31", branch=None).body)
    assert early["quantityErrors"] == [] and early["stockSpikes"] == []      # outside the date range
    assert cc._uom_factor("PACK@500PCS") == 500 and cc._uom_factor("GR") == 1
