"""Rebuild the integration_portal daily aggregates from integration_esb sales.

    python -m app.aggregates --recent 2                  # today + yesterday (WIB)
    python -m app.aggregates --from 2025-08-01 --to 2026-10-02
    python -m app.aggregates --from 2025-06-01 --months-only   # monthly rollups only

Each sales date is rebuilt in one transaction (delete + insert), so a run is
idempotent and readers never see a half-built day. Afterwards every month
touched is rolled up again into agg_menu_monthly and agg_hourly_monthly. The ESB engine updates at
most the last 7 days (hourly: today + yesterday, nightly: 7 days), which is
what the cron jobs in scripts/aggregates.cron refresh.

Fields used are 100% filled across the history (docs/overview-analytics.md §2).
"""
import argparse
import logging
import sys
import time
from datetime import date, timedelta

from app import database as db
from app.database import SCHEMA, TABLE_TRANSACTIONS
from app.esb_report import TYPE_CASE_SQL, TYPE_CONDITIONS, active_line_sql
from app.utils import today

logger = logging.getLogger("aggregates")
PORTAL = "integration_portal"
DAY_TIMEOUT_MS = 600_000


def _txt(expr: str, fallback: str = "Unknown") -> str:
    return f"COALESCE(NULLIF(TRIM({expr}), ''), '{fallback}')"


def _num(expr: str) -> str:
    return f"COALESCE(NULLIF({expr}, '')::numeric, 0)"


CHANNEL = _txt("h.raw_data->>'visitPurposeName'")
PAY_TYPE = _txt("h.raw_data->'salesPayments'->0->>'paymentMethodTypeName'")
PAY_METHOD = _txt("h.raw_data->'salesPayments'->0->>'paymentMethodName'")
DAY_FILTER = "h.sales_date >= %(day)s::date AND h.sales_date < %(day)s::date + 1"

SQL_DELETE = [f"DELETE FROM {PORTAL}.{t} WHERE sales_date = %(day)s"
              for t in ("agg_sales_daily", "agg_sales_hourly", "agg_menu_daily")]

SQL_DAILY = f"""
WITH bill AS (
    SELECT h.branch_code, {CHANNEL} AS channel, {PAY_TYPE} AS payment_type, {PAY_METHOD} AS payment_method,
           {TYPE_CASE_SQL} AS tx_type,
           h.subtotal, h.nett_sales, h.total_amount,
           {_num("h.raw_data->>'discountTotal'")} AS discount_total,
           {_num("h.raw_data->>'menuDiscountTotal'")} AS menu_discount,
           {_num("h.raw_data->>'promotionDiscount'")} AS promotion_discount,
           {_num("h.raw_data->>'voucherDiscountTotal'")} AS voucher_discount,
           m.lines, m.qty, m.has_bev, m.has_food
    FROM {TABLE_TRANSACTIONS} h
    CROSS JOIN LATERAL (
        SELECT count(*) AS lines,
               COALESCE(sum({_num("x->>'qty'")}), 0) AS qty,
               bool_or(x->>'menuCategoryName' = 'BEVERAGE') AS has_bev,
               bool_or(x->>'menuCategoryName' = 'FOOD') AS has_food
        FROM jsonb_array_elements(COALESCE(h.raw_data->'salesMenus', '[]'::jsonb)) x
        WHERE {active_line_sql('x')}
    ) m
    WHERE {DAY_FILTER}
)
INSERT INTO {PORTAL}.agg_sales_daily
SELECT %(day)s::date, branch_code, channel, payment_type, payment_method, tx_type,
       count(*), sum(subtotal), sum(nett_sales), sum(total_amount),
       sum(discount_total), sum(menu_discount), sum(promotion_discount), sum(voucher_discount),
       sum(lines), sum(qty),
       count(*) FILTER (WHERE has_bev), count(*) FILTER (WHERE has_food),
       count(*) FILTER (WHERE has_bev AND has_food)
FROM bill
GROUP BY branch_code, channel, payment_type, payment_method, tx_type
"""

SQL_HOURLY = f"""
INSERT INTO {PORTAL}.agg_sales_hourly
SELECT %(day)s::date, h.branch_code, {CHANNEL},
       COALESCE(NULLIF(substr(h.raw_data->>'salesDateIn', 12, 2), '')::smallint, 0) AS hour,
       count(*), sum(h.subtotal)
FROM {TABLE_TRANSACTIONS} h
WHERE {DAY_FILTER} AND {TYPE_CONDITIONS['sales']}
GROUP BY 2, 3, 4
"""

# menus + their packages/extras; add-on categories come from the POS menu master
SQL_MENU = f"""
WITH line AS (
    SELECT h.sales_num, h.branch_code, {CHANNEL} AS channel,
           'menu' AS kind, m->>'menuID' AS menu_id, m->>'menuName' AS menu_name,
           {_txt("m->>'menuCategoryName'")} AS category, {_txt("m->>'menuCategoryDetailName'")} AS category_detail,
           {_num("m->>'qty'")} AS qty, {_num("m->>'price'")} * {_num("m->>'qty'")} AS subtotal,
           {_num("m->>'discountValue'")} AS discount
    FROM {TABLE_TRANSACTIONS} h, jsonb_array_elements(COALESCE(h.raw_data->'salesMenus', '[]'::jsonb)) m
    WHERE {DAY_FILTER} AND {TYPE_CONDITIONS['sales']} AND {active_line_sql('m')}
    UNION ALL
    SELECT h.sales_num, h.branch_code, {CHANNEL}, a.kind, p->>'menuID', p->>'menuName',
           COALESCE(NULLIF(split_part(mm.raw_data->>'categoryDetail', ' - ', 1), ''), {_txt("m->>'menuCategoryName'")}),
           COALESCE(NULLIF(split_part(mm.raw_data->>'categoryDetail', ' - ', 2), ''), upper(a.kind)),
           {_num("p->>'qty'")}, {_num("p->>'price'")} * {_num("p->>'qty'")}, {_num("p->>'discountValue'")}
    FROM {TABLE_TRANSACTIONS} h,
         jsonb_array_elements(COALESCE(h.raw_data->'salesMenus', '[]'::jsonb)) m,
         LATERAL (VALUES ('package', m->'packages'), ('extra', m->'extras')) a(kind, items),
         jsonb_array_elements(COALESCE(a.items, '[]'::jsonb)) p
         LEFT JOIN {SCHEMA}.master_pos_menu mm ON mm.menu_id = p->>'menuID'
    WHERE {DAY_FILTER} AND {TYPE_CONDITIONS['sales']} AND {active_line_sql('m')}
)
INSERT INTO {PORTAL}.agg_menu_daily
SELECT %(day)s::date, branch_code, channel, COALESCE(menu_id, '?'), kind, max(menu_name), max(category),
       max(category_detail), count(DISTINCT sales_num), sum(qty), sum(subtotal), sum(discount)
FROM line
GROUP BY branch_code, channel, COALESCE(menu_id, '?'), kind
"""

SQL_MONTH = [
    f"DELETE FROM {PORTAL}.agg_menu_monthly WHERE month = %(month)s",
    f"""
INSERT INTO {PORTAL}.agg_menu_monthly
SELECT %(month)s::date, branch_code, channel, menu_id, kind, max(menu_name), max(category), max(category_detail),
       sum(bills), sum(qty), sum(subtotal), sum(discount)
FROM {PORTAL}.agg_menu_daily
WHERE sales_date >= %(month)s::date AND sales_date < (%(month)s::date + interval '1 month')::date
GROUP BY branch_code, channel, menu_id, kind
""",
    f"DELETE FROM {PORTAL}.agg_hourly_monthly WHERE month = %(month)s",
    f"""
INSERT INTO {PORTAL}.agg_hourly_monthly
SELECT %(month)s::date, branch_code, channel, extract(isodow FROM sales_date)::smallint, hour, sum(bills), sum(subtotal)
FROM {PORTAL}.agg_sales_hourly
WHERE sales_date >= %(month)s::date AND sales_date < (%(month)s::date + interval '1 month')::date
GROUP BY 2, 3, 4, 5
""",
]

SQL_LOG = f"""
INSERT INTO {PORTAL}.agg_refresh_log (sales_date, refreshed_at, source_synced_at, sales_bills, sales_subtotal, duration_ms)
SELECT %(day)s::date, now(),
       (SELECT max(h.synced_at) FROM {TABLE_TRANSACTIONS} h WHERE {DAY_FILTER}),
       COALESCE(sum(bills), 0), COALESCE(sum(subtotal), 0), %(ms)s
FROM {PORTAL}.agg_sales_daily WHERE sales_date = %(day)s AND tx_type = 'sales'
ON CONFLICT (sales_date) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at,
    source_synced_at = EXCLUDED.source_synced_at, sales_bills = EXCLUDED.sales_bills,
    sales_subtotal = EXCLUDED.sales_subtotal, duration_ms = EXCLUDED.duration_ms
"""

SQL_RECONCILE = f"""
SELECT (SELECT COALESCE(sum(subtotal), 0) FROM {PORTAL}.agg_sales_daily WHERE sales_date = %(day)s AND tx_type = 'sales') AS agg,
       (SELECT COALESCE(sum(h.subtotal), 0) FROM {TABLE_TRANSACTIONS} h WHERE {DAY_FILTER} AND {TYPE_CONDITIONS['sales']}) AS src
"""


def refresh_day(day: date) -> dict:
    """Rebuild every aggregate for one sales date; returns the reconciliation result."""
    t0 = time.monotonic()
    params = {"day": day.isoformat()}
    with db.transaction(timeout_ms=DAY_TIMEOUT_MS) as conn:
        for sql in SQL_DELETE:
            conn.execute(sql, params)
        conn.execute(SQL_DAILY, params)
        conn.execute(SQL_HOURLY, params)
        conn.execute(SQL_MENU, params)
        rec = conn.execute(SQL_RECONCILE, params).fetchone()
        ms = int((time.monotonic() - t0) * 1000)
        conn.execute(SQL_LOG, {**params, "ms": ms})
    ok = abs(float(rec["agg"]) - float(rec["src"])) < 0.5
    return {"day": day.isoformat(), "ms": ms, "subtotal": float(rec["agg"]), "ok": ok}


def refresh_month(month: date) -> None:
    """Roll one calendar month up into agg_menu_monthly and agg_hourly_monthly."""
    t0 = time.monotonic()
    params = {"month": month.replace(day=1).isoformat()}
    with db.transaction(timeout_ms=DAY_TIMEOUT_MS) as conn:
        for sql in SQL_MONTH:
            conn.execute(sql, params)
    logger.info("%s monthly rollups (%sms)", params["month"][:7], int((time.monotonic() - t0) * 1000))


def months_between(start: date, end: date) -> list[date]:
    months, m = [], start.replace(day=1)
    while m <= end:
        months.append(m)
        m = (m + timedelta(days=32)).replace(day=1)
    return months


def refresh_range(start: date, end: date, months_only: bool = False) -> int:
    """Rebuild [start, end] and the months it touches; returns the number of failures
    (days that failed or did not reconcile, months that failed).

    Runs are serialised by flock in scripts/aggregates.cron (and scripts/aggregates.sh).
    """
    failed = 0
    day = end + timedelta(days=1) if months_only else start
    while day <= end:
        try:
            r = refresh_day(day)
            status = "ok" if r["ok"] else "MISMATCH"
            failed += 0 if r["ok"] else 1
            logger.info("%s %s subtotal=%s (%sms)", r["day"], status, f"{r['subtotal']:,.0f}", r["ms"])
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("%s failed", day)
        day += timedelta(days=1)
    for month in months_between(start, end):
        try:
            refresh_month(month)
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("%s monthly rollup failed", month)
    return failed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recent", type=int, help="rebuild the last N days including today (WIB)")
    parser.add_argument("--from", dest="date_from", help="first sales date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="last sales date (YYYY-MM-DD), default today")
    parser.add_argument("--months-only", action="store_true", help="only rebuild the monthly rollups")
    args = parser.parse_args(argv)
    end = date.fromisoformat(args.date_to) if args.date_to else today()
    if args.recent:
        start = end - timedelta(days=args.recent - 1)
    elif args.date_from:
        start = date.fromisoformat(args.date_from)
    else:
        parser.error("use --recent N or --from YYYY-MM-DD")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.open_pool(wait=True)
    try:
        logger.info("refresh %s .. %s", start, end)
        failed = refresh_range(start, end, args.months_only)
        logger.info("done, %s failure(s)", failed)
        return 1 if failed else 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
