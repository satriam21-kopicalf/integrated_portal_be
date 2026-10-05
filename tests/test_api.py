"""Transactions/branches/summary API and ESB report rules (database stubbed, see conftest.py)."""
from datetime import date, datetime

from app import esb_report
from app.esb_report import REPORT_HEADERS, report_rows
from tests.conftest import SALES

COL = {n: i for i, n in enumerate(REPORT_HEADERS)}
MENUS = {"139": ("EXTRA", "LEVEL SUGAR")}
BRANCHES = {"CCI01": ("Kopi Calf Supratman Bandung", "Kopi Calf", "Bandung, Kota", None)}


def test_root(client):
    assert client.get("/").json()["status"] == "ok"


def test_esb_report_rows_rules():
    rows = report_rows(SALES[0]["raw_data"], MENUS, BRANCHES, {})
    assert len(rows) == 3 and all(len(r) == 46 for r in rows)
    menu, package, second = rows
    # package row: suffix + category from the POS menu master, zero amounts
    assert package[COL["Menu"]] == "Normal Sugar (PACKAGE)"
    assert (package[COL["Menu Category"]], package[COL["Menu Category Detail"]]) == ("EXTRA", "LEVEL SUGAR")
    assert package[COL["Subtotal"]] == 0 and package[COL["Bill Discount"]] == 0
    # bill discount 10.000 split by subtotal 25.000 : 75.000
    assert menu[COL["Bill Discount"]] == 2500 and second[COL["Bill Discount"]] == 7500
    assert menu[COL["Nett Sales"]] == 22500 and second[COL["Total After Bill Discount"]] == 67500
    assert second[COL["Subtotal"]] == 75000 and second[COL["Qty"]] == 3
    # header columns as ESB shows them
    assert menu[COL["Branch"]] == "Kopi Calf Supratman Bandung"  # current master name, not raw_data
    assert menu[COL["City"]] == "Bandung, Kota"
    assert menu[COL["Payment Method"]] == "Debit Card (23.100),Qris (66.900)"
    assert menu[COL["Waiter"]] == "CCI01 KASIR1"  # naming-pattern fallback
    assert menu[COL["Regular Member Code"]] == "Non Member" and menu[COL["Customer Name"]] == "-"
    assert menu[COL["Sales Date"]] == date(2026, 9, 30)
    assert menu[COL["Sales Date In"]] == datetime(2026, 9, 30, 10, 0, 0)
    assert menu[COL["Sales Type"]] == "Sales"


def test_waiter_uses_user_master():
    rows = report_rows(SALES[2]["raw_data"], MENUS, BRANCHES, {"CALFTGP17KASIR1": "Kasir Pamulang"})
    assert rows[0][COL["Waiter"]] == "Kasir Pamulang"


def test_list_defaults_to_esb_sales(client):
    body = client.get("/api/transactions", params={"dateFrom": "2026-09-01", "dateTo": "2026-09-30"}).json()
    assert body["type"] == "sales"
    assert [r["sales_num"] for r in body["data"]] == ["S-003", "S-003", "S-003", "S-000"]
    first = body["data"][0]
    assert first["branch_name"] == "Kopi Calf Supratman Bandung"
    assert first["sales_date_in"] == "2026-09-30T10:00:00"  # WIB wall clock, no UTC shift
    assert first["line_number"] == 1 and body["data"][1]["menu_name"] == "Normal Sugar (PACKAGE)"
    assert body["summary"]["totalHeaders"] == 2
    assert body["pagination"] == {"cursor": None, "hasMore": False, "limit": 100}


def test_list_types(client):
    get = lambda t: {r["sales_num"] for r in client.get("/api/transactions", params={"type": t}).json()["data"]}  # noqa: E731
    assert get("void") == {"S-002"}
    assert get("other_cost") == {"S-001"}
    assert get("all") == {"S-000", "S-001", "S-002", "S-003"}
    assert get("bogus") == {"S-000", "S-003"}  # unknown -> sales


def test_list_branch_filter_uses_code(client, fake_db):
    client.get("/api/transactions", params={"branch": "CCI01"})
    query, params = next(q for q in fake_db.queries if "h.raw_data FROM" in q[0])
    assert "h.branch_code = ANY(%s)" in query and ["CCI01"] in params


def test_list_several_branches(client, fake_db):
    def nums(branch):
        rows = client.get("/api/transactions", params={"branch": branch, "type": "all", "dateFrom": "2026-09-01"}).json()["data"]
        return {r["sales_num"] for r in rows}
    assert nums("TGP17") == {"S-001"}
    assert nums("TGP17, CCI01") == {"S-000", "S-001", "S-002", "S-003"}
    query, params = [q for q in fake_db.queries if "h.raw_data FROM" in q[0]][-1]
    assert ["CCI01", "TGP17"] in params            # normalised: trimmed + sorted


def test_parse_branches():
    from app.utils import normalize_branch, parse_branches
    assert parse_branches(" CCI04,CCI01,,CCI04 ") == ["CCI01", "CCI04"]
    assert parse_branches(None) == [] and normalize_branch("") is None
    assert normalize_branch("TGP17,CCI01") == "CCI01,TGP17"


def test_cursor_pagination(client, fake_db):
    body = client.get("/api/transactions", params={"limit": 1, "dateFrom": "2026-09-01"}).json()
    assert body["pagination"]["hasMore"] is True
    cursor = body["pagination"]["cursor"]
    assert cursor.endswith("|||S-003")
    client.get("/api/transactions", params={"limit": 1, "cursor": cursor})
    assert any("(h.sales_date, h.sales_num) < (%s, %s)" in q for q, _ in fake_db.queries)


def test_search_escapes_like(client, fake_db):
    client.get("/api/transactions", params={"search": "50%_off"})
    query, params = next(q for q in fake_db.queries if "ILIKE" in q[0])
    assert "%50\\%\\_off%" in params


def test_summary_deductions(client):
    s = client.get("/api/summary", params={"dateFrom": "2026-09-30", "dateTo": "2026-09-30"}).json()
    t = s["totals"]
    assert t["gross"]["subtotal"] == 100000 + 30000 + 50000
    assert t["void"]["subtotal"] == 30000
    assert t["other_cost"]["subtotal"] == 50000
    assert t["sales"]["subtotal"] == 100000 and t["sales"]["nettSales"] == 90000
    assert t["gross"]["subtotal"] - t["void"]["subtotal"] - t["other_cost"]["subtotal"] - t["open"]["subtotal"] \
        == t["sales"]["subtotal"]
    assert s["otherCostByMethod"] == {"CUPPING": {"transactions": 1, "subtotal": 50000, "nettSales": 50000, "total": 50000}}
    assert [d["date"] for d in s["days"]] == ["2026-09-30"]


def test_branches(client):
    assert client.get("/api/branches").json() == [
        {"branch_code": "CCI01", "branch_name": "Kopi Calf Supratman Bandung", "count": 2}]


def test_transaction_detail_and_404(client):
    body = client.get("/api/transactions/S-003").json()
    assert body["sales_num"] == "S-003" and body["branch_name"] == "Kopi Calf Supratman Bandung"
    assert len(body["report_rows"]) == 3 and body["items"]
    assert client.get("/api/transactions/NOPE").json() == {"error": "Transaction not found"}


def test_transaction_detail_with_slash_in_id(client, fake_db):
    client.get("/api/transactions/ABC%2F123")
    assert any(p == ("ABC/123",) for _, p in fake_db.queries)


def test_old_truncating_export_endpoint_removed(client):
    assert client.post("/api/transactions/export", json={}).status_code in (404, 405)


def test_cancelled_menu_lines_are_left_out():
    from tests.conftest import MASTERS, menu, sale
    cancelled = menu("Classic Milk Tea", 2, 25000, menu_id=252)
    cancelled.update(statusID="19", statusName="Print Cancelled", cancelNotes="Test Order")
    h = sale("S-9", "B-9", "2026-10-02", "Finished", [menu("Es Kopi Calf Premium", 1, 20000), cancelled])
    raw = h["raw_data"]
    raw["subtotal"] = raw["grandTotal"] = 20000  # ESB's subtotal never includes cancelled lines
    menus = {m["menu_id"]: ("EXTRA", "LEVEL SUGAR") for m in MASTERS["menus"]}
    rows = esb_report.report_rows(raw, menus, {}, {})
    assert [r[27] for r in rows] == ["Es Kopi Calf Premium"]
    assert sum(r[34] for r in rows) == 20000
    assert esb_report.is_cancelled_line(cancelled) and not esb_report.is_cancelled_line(raw["salesMenus"][0])
    assert "'19'" in esb_report.active_line_sql("m") and "%%cancel%%" in esb_report.active_line_sql("m")
