"""Build the Cost Control aggregates (integration_portal.agg_cost_period / agg_cost_item_period).

    python -m app.cost_control --recent-days 10          # periods overlapping the last 10 days
    python -m app.cost_control --from 2025-11-01         # every period since

Periods follow the outlet stock-opname rhythm (days 1-7, 8-14, 15-21, 22-end). One
period is rebuilt in one transaction (delete + insert). Sources, all in integration_esb
(synced by integrated-esbapi):

  inventory_valuation   ESB valuation per location x product x period (HPP): begin,
                        purchases, theoretical usage (POS sales x BOM = "sales"), other
                        usage (item journal), manufacturing, posted opname adjustments, end
  erp_documents/_lines  stock opname documents; those not posted yet in ESB (Draft/New)
                        give the pending variance: sum((physical - system qty) x HPP)
  master_locations / master_branches / master_products / master_product_units
and the portal's agg_sales_daily (ESB "Sales": net sales, subtotal; "other cost" bills).

Sign convention of the aggregates: costs and usage are positive; variance is negative
for a loss (physical stock below system stock).

  actual COGS = theoretical + other usage + manufacturing net - posted variance - pending variance

A pending (Draft/New) opname line whose variance is above max(SUSPECT_FLOOR, SUSPECT_SHARE x the
outlet's theoretical COGS of the period) is implausible (e.g. a system stock of 29 tonnes of
espresso) and left out of the pending variance; it is kept as excluded_pending_variance and listed
by /api/cost-control/issues. Posted opnames are never excluded.
"""
import argparse
import logging
import sys
import time
from datetime import date, timedelta

from app import database as db
from app.database import SCHEMA
from app.utils import today

logger = logging.getLogger("cost_control")
PORTAL = "integration_portal"
TIMEOUT_MS = 600_000
POSTED_STATUSES = ("Authorized", "Finished", "Closed")
SUSPECT_FLOOR = 50_000_000   # Rp
SUSPECT_SHARE = 0.5          # of the outlet's theoretical COGS in the period
IGNORED_STATUSES = ("Rejected", "Cancelled", "Void")


def period_bounds(day: date) -> tuple[date, date]:
    """The opname-aligned period containing `day`."""
    month_end = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    for first, last in ((1, 7), (8, 14), (15, 21)):
        if first <= day.day <= last:
            return day.replace(day=first), day.replace(day=last)
    return day.replace(day=22), month_end


def periods_between(start: date, end: date) -> list[tuple[date, date]]:
    out, day = [], period_bounds(start)[0]
    while day <= end:
        p = period_bounds(day)
        out.append(p)
        day = p[1] + timedelta(days=1)
    return out


# outlet location -> branch (location type 3 = outlet/store)
LOCATIONS = f"""
SELECT DISTINCT ON (l.location_id) l.location_id, b.branch_code
FROM {SCHEMA}.master_locations l
JOIN {SCHEMA}.master_branches b ON b.branch_id = l.branch_id
WHERE l.location_type = '3' AND COALESCE(b.branch_code, '') <> ''
ORDER BY l.location_id, COALESCE(b.is_deleted, false)
"""

# opname lines with their variance; pending = document not posted in ESB yet
OPNAME_LINES = f"""
SELECT d.location_id, d.doc_num, d.doc_date, d.status_name,
       d.status_name NOT IN {POSTED_STATUSES!r} AS pending,
       l.product_id, l.qty AS physical_qty,
       COALESCE(NULLIF(l.raw_data->>'stockQty', '')::numeric, 0) AS system_qty,
       COALESCE(l.price, 0) AS hpp
FROM {SCHEMA}.erp_documents d
JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num
WHERE d.module = 'stock_opname' AND d.deleted_at IS NULL
  AND d.status_name NOT IN {IGNORED_STATUSES!r}
  AND d.doc_date BETWEEN %(ps)s AND %(pe)s
"""

SQL_DELETE = [
    f"DELETE FROM {PORTAL}.agg_cost_period WHERE period_start = %(ps)s",
    f"DELETE FROM {PORTAL}.agg_cost_item_period WHERE period_start = %(ps)s",
]

SQL_PERIOD = f"""
WITH loc AS ({LOCATIONS}),
runs AS (
    SELECT location_id, period_end FROM {SCHEMA}.inventory_valuation_runs WHERE period_start = %(ps)s
),
val AS (
    SELECT v.location_id,
           sum(COALESCE(v.beginning_hpp, 0)) AS begin_value,
           sum(COALESCE(v.purchase_hpp, 0) + COALESCE(v.purchase_return_hpp, 0) + COALESCE(v.goods_receipt_return_hpp, 0)
               + COALESCE(v.invoice_adjustment_in_hpp, 0) + COALESCE(v.invoice_adjustment_out_hpp, 0)) AS purchase_value,
           sum(COALESCE(v.transfer_in_hpp, 0)) AS transfer_in_value,
           -sum(COALESCE(v.transfer_out_hpp, 0)) AS transfer_out_value,
           -sum(COALESCE(v.sales_hpp, 0) + COALESCE(v.sales_return_hpp, 0)) AS theoretical_cogs,
           -sum(COALESCE(v.other_out_hpp, 0) + COALESCE(v.other_in_hpp, 0)) AS other_usage,
           -sum(COALESCE(v.manufacturing_out_hpp, 0) + COALESCE(v.manufacturing_in_hpp, 0)) AS manufacturing_net,
           sum(COALESCE(v.opname_in_hpp, 0) + COALESCE(v.opname_out_hpp, 0)) AS posted_variance,
           sum(COALESCE(v.end_hpp, 0)) AS end_value
    FROM {SCHEMA}.inventory_valuation v
    WHERE v.period_start = %(ps)s
    GROUP BY v.location_id
),
opn_lines AS (
    SELECT o.*, (o.physical_qty - o.system_qty) * o.hpp AS variance,
           o.pending AND abs((o.physical_qty - o.system_qty) * o.hpp)
               > GREATEST(%(floor)s, %(share)s * COALESCE(val.theoretical_cogs, 0)) AS suspect
    FROM ({OPNAME_LINES}) o LEFT JOIN val ON val.location_id = o.location_id
),
opn AS (
    SELECT location_id,
           count(DISTINCT doc_num)::int AS opname_count,
           count(DISTINCT doc_num) FILTER (WHERE pending)::int AS pending_opname_count,
           max(doc_date) AS last_opname_date,
           COALESCE(sum(variance) FILTER (WHERE pending AND NOT suspect), 0) AS pending_variance,
           COALESCE(sum(variance) FILTER (WHERE suspect), 0) AS excluded_pending_variance,
           count(*) FILTER (WHERE suspect)::int AS excluded_pending_lines
    FROM opn_lines
    GROUP BY location_id
),
sales AS (
    SELECT branch_code,
           COALESCE(sum(bills) FILTER (WHERE tx_type = 'sales'), 0)::int AS bills,
           COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'sales'), 0) AS subtotal,
           COALESCE(sum(nett_sales) FILTER (WHERE tx_type = 'sales'), 0) AS net_sales,
           COALESCE(sum(subtotal) FILTER (WHERE tx_type = 'other_cost'), 0) AS other_cost_subtotal
    FROM {PORTAL}.agg_sales_daily
    WHERE sales_date BETWEEN %(ps)s AND %(pe)s
    GROUP BY branch_code
)
INSERT INTO {PORTAL}.agg_cost_period (
    branch_code, period_start, period_end, location_id, bills, subtotal, net_sales, other_cost_subtotal,
    begin_value, purchase_value, transfer_in_value, transfer_out_value, theoretical_cogs, other_usage,
    manufacturing_net, posted_variance, pending_variance, end_value, actual_cogs,
    opname_count, pending_opname_count, last_opname_date, excluded_pending_variance, excluded_pending_lines, refreshed_at)
SELECT loc.branch_code, %(ps)s, COALESCE(runs.period_end, %(pe)s), loc.location_id,
       COALESCE(s.bills, 0), COALESCE(s.subtotal, 0), COALESCE(s.net_sales, 0), COALESCE(s.other_cost_subtotal, 0),
       val.begin_value, val.purchase_value, val.transfer_in_value, val.transfer_out_value, val.theoretical_cogs,
       val.other_usage, val.manufacturing_net, val.posted_variance, COALESCE(opn.pending_variance, 0), val.end_value,
       val.theoretical_cogs + val.other_usage + val.manufacturing_net - val.posted_variance - COALESCE(opn.pending_variance, 0),
       COALESCE(opn.opname_count, 0), COALESCE(opn.pending_opname_count, 0), opn.last_opname_date,
       COALESCE(opn.excluded_pending_variance, 0), COALESCE(opn.excluded_pending_lines, 0), now()
FROM val
JOIN loc ON loc.location_id = val.location_id
LEFT JOIN runs ON runs.location_id = val.location_id
LEFT JOIN opn ON opn.location_id = val.location_id
LEFT JOIN sales s ON s.branch_code = loc.branch_code
ON CONFLICT (branch_code, period_start) DO NOTHING
"""

SQL_ITEMS = f"""
WITH loc AS ({LOCATIONS}),
theo AS (
    SELECT location_id, -sum(COALESCE(sales_hpp, 0) + COALESCE(sales_return_hpp, 0)) AS theoretical_cogs
    FROM {SCHEMA}.inventory_valuation WHERE period_start = %(ps)s GROUP BY 1
),
pending AS (
    SELECT o.location_id, o.product_id,
           sum(o.physical_qty - o.system_qty) AS qty,
           sum((o.physical_qty - o.system_qty) * o.hpp) AS value
    FROM ({OPNAME_LINES}) o LEFT JOIN theo ON theo.location_id = o.location_id
    WHERE o.pending
      AND abs((o.physical_qty - o.system_qty) * o.hpp) <= GREATEST(%(floor)s, %(share)s * COALESCE(theo.theoretical_cogs, 0))
    GROUP BY 1, 2
),
base_unit AS (
    SELECT DISTINCT ON (product_id) product_id, uom_name
    FROM {SCHEMA}.master_product_units
    ORDER BY product_id, is_base DESC, qty
),
v AS (
    SELECT loc.branch_code, v.location_id, v.product_id, max(v.product_name) AS product_name,
           sum(COALESCE(v.beginning_qty, 0)) AS begin_qty,
           sum(COALESCE(v.purchase_qty, 0) + COALESCE(v.purchase_return_qty, 0) + COALESCE(v.goods_receipt_return_qty, 0)) AS purchase_qty,
           sum(COALESCE(v.purchase_hpp, 0) + COALESCE(v.purchase_return_hpp, 0) + COALESCE(v.goods_receipt_return_hpp, 0)
               + COALESCE(v.invoice_adjustment_in_hpp, 0) + COALESCE(v.invoice_adjustment_out_hpp, 0)) AS purchase_value,
           -sum(COALESCE(v.sales_qty, 0) + COALESCE(v.sales_return_qty, 0)) AS theoretical_qty,
           -sum(COALESCE(v.sales_hpp, 0) + COALESCE(v.sales_return_hpp, 0)) AS theoretical_value,
           -sum(COALESCE(v.other_out_qty, 0) + COALESCE(v.other_in_qty, 0)) AS other_qty,
           -sum(COALESCE(v.other_out_hpp, 0) + COALESCE(v.other_in_hpp, 0)) AS other_value,
           -sum(COALESCE(v.manufacturing_out_qty, 0) + COALESCE(v.manufacturing_in_qty, 0)) AS manufacturing_qty,
           -sum(COALESCE(v.manufacturing_out_hpp, 0) + COALESCE(v.manufacturing_in_hpp, 0)) AS manufacturing_value,
           sum(COALESCE(v.opname_in_qty, 0) + COALESCE(v.opname_out_qty, 0)) AS posted_qty,
           sum(COALESCE(v.opname_in_hpp, 0) + COALESCE(v.opname_out_hpp, 0)) AS posted_value,
           sum(COALESCE(v.end_qty, 0)) AS end_qty,
           sum(COALESCE(v.end_hpp, 0)) AS end_value
    FROM {SCHEMA}.inventory_valuation v
    JOIN loc ON loc.location_id = v.location_id
    WHERE v.period_start = %(ps)s
    GROUP BY 1, 2, 3
)
INSERT INTO {PORTAL}.agg_cost_item_period (
    branch_code, period_start, product_id, product_code, product_name, category, base_unit,
    begin_qty, purchase_qty, purchase_value, theoretical_qty, theoretical_value, other_qty, other_value,
    variance_qty, variance_value, actual_qty, actual_value, end_qty, end_value)
SELECT v.branch_code, %(ps)s, v.product_id, p.product_code, COALESCE(p.product_name, v.product_name, v.product_id),
       c.category_name, bu.uom_name,
       v.begin_qty, v.purchase_qty, v.purchase_value, v.theoretical_qty, v.theoretical_value, v.other_qty, v.other_value,
       v.posted_qty + COALESCE(pd.qty, 0), v.posted_value + COALESCE(pd.value, 0),
       v.theoretical_qty + v.other_qty + v.manufacturing_qty - v.posted_qty - COALESCE(pd.qty, 0),
       v.theoretical_value + v.other_value + v.manufacturing_value - v.posted_value - COALESCE(pd.value, 0),
       v.end_qty, v.end_value
FROM v
LEFT JOIN pending pd ON pd.location_id = v.location_id AND pd.product_id = v.product_id
LEFT JOIN {SCHEMA}.master_products p ON p.product_id = v.product_id
LEFT JOIN {SCHEMA}.master_product_categories c ON c.category_id = p.category_id
LEFT JOIN base_unit bu ON bu.product_id = v.product_id
ON CONFLICT (branch_code, period_start, product_id) DO NOTHING
"""


def refresh_period(ps: date, pe: date) -> dict:
    t0 = time.monotonic()
    params = {"ps": ps.isoformat(), "pe": min(pe, today() - timedelta(days=1)).isoformat(),
              "floor": SUSPECT_FLOOR, "share": SUSPECT_SHARE}
    with db.transaction(timeout_ms=TIMEOUT_MS) as conn:
        for sql in SQL_DELETE:
            conn.execute(sql, params)
        outlets = conn.execute(SQL_PERIOD, params).rowcount
        items = conn.execute(SQL_ITEMS, params).rowcount
    return {"period": f"{ps}..{pe}", "outlets": outlets, "items": items, "ms": int((time.monotonic() - t0) * 1000)}


def refresh_range(start: date, end: date) -> int:
    failed = 0
    for ps, pe in periods_between(start, end):
        if ps >= today():
            continue
        try:
            r = refresh_period(ps, pe)
            logger.info("%s outlets=%s items=%s (%sms)", r["period"], r["outlets"], r["items"], r["ms"])
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("%s..%s failed", ps, pe)
    return failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recent-days", type=int, help="rebuild the periods overlapping the last N days")
    parser.add_argument("--from", dest="date_from", help="first day (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="last day (YYYY-MM-DD), default yesterday")
    args = parser.parse_args(argv)
    end = date.fromisoformat(args.date_to) if args.date_to else today() - timedelta(days=1)
    if args.recent_days:
        start = end - timedelta(days=args.recent_days - 1)
    elif args.date_from:
        start = date.fromisoformat(args.date_from)
    else:
        parser.error("use --recent-days N or --from YYYY-MM-DD")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.open_pool(wait=True)
    try:
        logger.info("cost control refresh %s .. %s", start, end)
        failed = refresh_range(start, end)
        logger.info("done, %s failure(s)", failed)
        return 1 if failed else 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
