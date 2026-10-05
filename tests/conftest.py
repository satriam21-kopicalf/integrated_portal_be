"""Shared fixtures: the database layer is replaced by an in-memory fake holding
ESB-shaped raw_data, so the ESB report rules are exercised end to end."""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app import esb_report, exports
from app.config import get_settings
from app.main import app
from app.routes import branches as branches_module
from app.routes import transactions as tx_module
from app.routes.auth import current_user

SIGNED_IN = {"id": "00000000-0000-0000-0000-000000000001", "username": "tester", "email": "tester@kopicalf.co.id",
             "full_name": "Test Admin", "role": "superadmin", "is_active": True}


def menu(name, qty, price, *, detail="ORIGINAL KOPI SUSU", discount=0, vat=0, packages=(), batch="1",
         sales_type="POS Lite", created="2026-09-30T10:00:01+07:00", menu_id=1):
    return {
        "menuID": str(menu_id), "menuName": name, "qty": qty, "price": price, "discountValue": discount,
        "vatValue": vat, "otherTaxValue": 0, "otherVatValue": 0, "total": price * qty - discount + vat,
        "menuCategoryName": "BEVERAGE", "menuCategoryDetailName": detail, "batchID": batch,
        "salesType": sales_type, "createdDate": created, "notes": "", "menuCode": "",
        "packages": [{"menuID": str(p[0]), "menuName": p[1], "qty": qty, "price": 0, "total": 0,
                      "discountValue": 0, "vatValue": 0, "otherTaxValue": 0, "otherVatValue": 0} for p in packages],
        "extras": [],
    }


def sale(sales_num, bill, day, status, menus, *, payments=(("Qris", None),), discount_total=0, branch="CCI01",
         created_by="CALFCCI01KASIR1", full_name=None):
    subtotal = sum(m["price"] * m["qty"] for m in menus)
    grand = subtotal - discount_total
    raw = {
        "salesNum": sales_num, "billNum": bill, "salesDate": day, "statusName": status,
        "salesDateIn": f"{day} 10:00:00", "salesDateOut": f"{day} 10:05:00",
        "branchCode": branch, "branchName": "Old Branch Name", "tableName": "Quick Service",
        "visitPurposeName": "Dine In", "memberCode": "", "memberName": None, "fullName": full_name,
        "createdBy": created_by, "subtotal": subtotal, "grandTotal": grand, "discountTotal": discount_total,
        "menuDiscountTotal": 0, "promotionDiscount": 0, "voucherDiscountTotal": 0,
        "salesPayments": [{"paymentMethodName": m, "paymentAmount": a if a is not None else grand}
                          for m, a in payments],
        "salesMenus": menus,
    }
    return {
        "sales_num": sales_num, "bill_num": bill or "", "sales_date": datetime(*map(int, day.split("-"))),
        "branch_code": branch, "branch_name": "Old Branch Name", "status": status,
        "subtotal": Decimal(subtotal), "nett_sales": Decimal(grand), "total_amount": Decimal(grand),
        "payment_method": payments[0][0], "raw_data": raw,
    }


SALES = [
    # ESB sale with a package and a bill discount split by subtotal (25.000 : 75.000)
    sale("S-003", "B-3", "2026-09-30", "Finished",
         [menu("Es Kopi Calf Premium", 1, 25000, packages=[(139, "Normal Sugar")]),
          menu("Butterscotch Macchiato", 3, 25000, menu_id=11)],
         discount_total=10000, payments=(("Debit Card", 23100), ("Qris", 66900))),
    sale("S-002", "B-2", "2026-09-30", "Void", [menu("Latte", 1, 30000)]),
    sale("S-001", "", "2026-09-30", "Finished", [menu("Cupping Beans", 1, 50000)],
         payments=(("CUPPING", None),), created_by="CALFTGP17KASIR1", branch="TGP17"),
    sale("S-000", "B-0", "2026-09-29", "Finished", [menu("Tea", 2, 10000)]),
]
MASTERS = {
    "menus": [{"menu_id": "139", "category_name": "EXTRA", "raw_data": {"categoryDetail": "EXTRA - LEVEL SUGAR"}}],
    "branches": [{"branch_code": "CCI01", "branch_name": "Kopi Calf Supratman Bandung", "brand": "Kopi Calf",
                  "city": "Bandung, Kota", "area": None},
                 {"branch_code": "TGP17", "branch_name": "Kopi Calf To Go Pamulang", "brand": "Kopi Calf",
                  "city": None, "area": None}],
    "users": [{"user_name": "CALFTGP17KASIR1", "display_name": "Kasir Pamulang"}],
}


def _type_of(h):
    if h["status"] == "Finished" and h["bill_num"]:
        return "sales"
    if h["status"] in ("Void", "Cancelled"):
        return "void"
    return "other_cost" if h["status"] == "Finished" else "open"


def _query_type(query):
    for name, cond in esb_report.TYPE_CONDITIONS.items():
        if name != "all" and cond in query:
            return name
    return "all"


class FakeDB:
    def __init__(self):
        self.queries: list[tuple[str, object]] = []
        self.fail = False

    def fetch(self, query, params=None):
        self.queries.append((query, params))
        if self.fail:
            raise RuntimeError("database is down")
        if "integration_portal." in query:  # aggregates: none in the fake, so summaries read raw rows
            return []
        if "master_pos_menu" in query:
            return MASTERS["menus"]
        if "master_branch_attributes" in query:
            return MASTERS["branches"]
        if "master_pos_users" in query:
            return MASTERS["users"]
        if "WITH c AS" in query:  # /api/branches
            return [{"branch_code": "CCI01", "branch_name": "Kopi Calf Supratman Bandung", "count": 2}]
        if "WHERE branch_code = ANY(%s)" in query:
            return [b for b in MASTERS["branches"] if b["branch_code"] in params[0]]
        if "transactions_pos_sales_items" in query:
            return [{"sales_num": params[0], "line_number": 2, "menu_name": "Es Kopi Calf Premium"}]
        if "h.sales_num = %s" in query:
            return [h for h in SALES if h["sales_num"] == params[0]]

        rows = list(SALES)
        if "GROUP BY h.branch_code, b.branch_name" in query:  # daily report
            wanted = _query_type(query)
            day = params[0]
            groups = {}
            for h in rows:
                if h["sales_date"].date().isoformat() != day or (wanted != "all" and _type_of(h) != wanted):
                    continue
                if "h.branch_code = ANY(%s)" in query and h["branch_code"] not in params[2]:
                    continue
                raw = h["raw_data"]
                name = next((b["branch_name"] for b in MASTERS["branches"] if b["branch_code"] == h["branch_code"]), None)
                g = groups.setdefault(h["branch_code"], {"branch": name, **{k: Decimal(0) for k in (
                    "bills", "pax", "subtotal", "discount", "menu_discount", "voucher_discount", "service", "tax",
                    "vat", "delivery", "order_fee", "rounding", "grand", "voucher_sales")}})
                g["bills"] += 1
                g["pax"] += 1
                g["subtotal"] += Decimal(raw["subtotal"])
                g["discount"] += Decimal(raw["discountTotal"])
                g["grand"] += Decimal(raw["grandTotal"])
            return sorted(groups.values(), key=lambda g: g["branch"])
        if "GROUP BY 1, 2, 3" in query:  # summary
            out = {}
            for h in rows:
                if not (params[0] <= h["sales_date"].date().isoformat() <= params[1]):
                    continue
                kind = _type_of(h)
                method = h["payment_method"] if kind == "other_cost" else None
                key = (h["sales_date"].date().isoformat(), kind, method)
                r = out.setdefault(key, {"day": key[0], "kind": kind, "method": method, "n": 0,
                                         "subtotal": Decimal(0), "nett": Decimal(0), "total": Decimal(0)})
                r["n"] += 1
                r["subtotal"] += h["subtotal"]
                r["nett"] += h["nett_sales"]
                r["total"] += h["total_amount"]
            return sorted(out.values(), key=lambda r: r["day"])

        wanted = _query_type(query)
        rows = [h for h in rows if wanted == "all" or _type_of(h) == wanted]
        if "h.sales_date < %s" in query and "::date + 1" not in query:  # export: one day
            rows = [h for h in rows if h["sales_date"].date() == date.fromisoformat(params[0])]
        if "h.branch_code = ANY(%s)" in query:
            wanted_branches = next(p for p in params if isinstance(p, list))
            rows = [h for h in rows if h["branch_code"] in wanted_branches]
        if "LIMIT %s" in query:
            rows = rows[: params[-1]]
        return rows

    def fetchrow(self, query, params=None):
        rows = self.fetch(query, params)
        return rows[0] if rows else None


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    fake = FakeDB()
    monkeypatch.setattr(db, "fetch", fake.fetch)
    monkeypatch.setattr(db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(db, "open_pool", lambda: None)
    monkeypatch.setattr(db, "close_pool", lambda: None)
    monkeypatch.setattr(db, "stream", lambda query, params=None, size=0: iter(fake.fetch(query, params)))
    # run export jobs inline (instead of a separate process) so tests can assert on the file
    monkeypatch.setattr(exports, "_launch", lambda job: exports._run(job))
    monkeypatch.setattr(get_settings(), "export_dir", str(tmp_path / "exports"))
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    esb_report._master["loaded"] = 0.0
    tx_module._cache = tx_module.TTLCache()
    branches_module._cache = branches_module.TTLCache()
    return fake


@pytest.fixture
def client(fake_db):
    """API client signed in as a superadmin (auth itself is tested in test_auth.py)."""
    app.dependency_overrides[current_user] = lambda: SIGNED_IN
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(current_user, None)
