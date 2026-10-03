"""PostgreSQL connection pool (psycopg 3).

Routes are plain `def` functions, so FastAPI runs them in its threadpool and
the synchronous pool is safe to use. This also avoids psycopg's async
limitations with the Windows ProactorEventLoop during local development.
"""
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

SCHEMA = "integration_esb"
TABLE_TRANSACTIONS = f"{SCHEMA}.transactions_pos_sales"
TABLE_ITEMS = f"{SCHEMA}.transactions_pos_sales_items"

# Explicit column lists: both tables also carry a heavy `raw_data` JSON column
# that the portal never needs, so `SELECT *` is avoided.
HEADER_COLUMNS = ", ".join([
    "id", "sales_num", "bill_num", "sales_type", "batch_order", "table_section", "table_name",
    "sales_date", "sales_date_in", "sales_date_out", "sales_date_wib",
    "branch_id", "branch_code", "branch_name", "brand", "city", "area",
    "visit_purpose", "visit_purpose_id", "regular_member_code", "regular_member_name",
    "loyalty_member_code", "loyalty_member_name", "loyalty_member_type",
    "employee_code", "employee_name", "external_employee_code", "external_employee_name",
    "customer_name", "customer_email", "customer_phone",
    "subtotal", "discount_amount", "service_charge", "tax_amount", "vat_amount", "total_amount",
    "nett_sales", "dpp", "bill_discount", "total_after_discount",
    "payment_method", "cash_received", "change_given", "cashier_id", "waiter", "order_mode",
    "status", "status_id", "void_reason", "promotion_id", "promotion_name", "pax_total", "shift_id",
    "created_by", "updated_by", "created_at", "updated_at", "synced_at",
])
ITEM_COLUMNS = ", ".join([
    "id", "sales_id", "sales_num", "line_number", "batch_id",
    "menu_id", "menu_code", "menu_name", "custom_menu_name", "menu_notes",
    "menu_category_id", "menu_category", "menu_category_detail_id", "menu_category_detail",
    "quantity", "unit_price", "original_price", "subtotal", "discount_amount", "total",
    "order_time", "order_status_id", "order_status_name", "created_at", "updated_at", "synced_at",
])

_pool: Optional[ConnectionPool] = None


def _configure(conn: Connection) -> None:
    timeout = get_settings().db_statement_timeout_ms
    conn.execute(f"SET statement_timeout = {int(timeout)}")


def open_pool(wait: bool = False) -> None:
    global _pool
    if _pool is not None:
        return
    s = get_settings()
    conninfo = make_conninfo(
        host=s.db_host,
        port=s.db_port,
        dbname=s.db_name,
        user=s.db_user,
        password=s.db_password,
        sslmode=s.db_sslmode,
        connect_timeout=15,
        application_name="integrated_portal_be",
    )
    _pool = ConnectionPool(
        conninfo,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": True},
        configure=_configure,
        open=False,
    )
    # The API doesn't block startup if the DB is temporarily unreachable (/health
    # reports it); CLI jobs pass wait=True.
    _pool.open(wait=wait)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close(timeout=5)
        _pool = None


@contextmanager
def transaction(timeout_ms: Optional[int] = None) -> Iterator[Connection]:
    """A pooled connection inside one transaction (commit on success, rollback on error)."""
    if _pool is None:
        open_pool()
    with _pool.connection() as conn:
        with conn.transaction():
            if timeout_ms:
                conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            yield conn


def stream(query: str, params: Any = None, size: int = 2000) -> Iterator[dict]:
    """Rows of a large result in chunks of `size` (server-side cursor), so the
    whole result never sits in memory at once."""
    if _pool is None:
        open_pool()
    with _pool.connection() as conn:
        with conn.transaction():
            with conn.cursor(name=f"stream_{uuid.uuid4().hex[:12]}") as cur:
                cur.itersize = size
                cur.execute(query, params)
                yield from cur


def fetch(query: str, params: Any = None) -> list[dict]:
    if _pool is None:
        open_pool()
    with _pool.connection() as conn:
        return conn.execute(query, params).fetchall()


def fetchrow(query: str, params: Any = None) -> Optional[dict]:
    rows = fetch(query, params)
    return rows[0] if rows else None
