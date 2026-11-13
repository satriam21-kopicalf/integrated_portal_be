"""Pusat Informasi of Cost Control (page cc.info, 2026-11-13): how complete and how fresh the data behind the COGS,
the Usage Ratio and the Purchase Estimate is — one card per source, each with its status, the figures, what it means
for the numbers, where it comes from (ESB / Portal) and the page that fixes it. The same checks feed the notification
bell (app/alerts.py `health_alerts`) and the "Sources" line of every Cost Control page (`sources`).

    esbSales     ESB POS sales rolled up for the portal (agg_refresh_log): the last day and the last sync
    esbDocs      ESB ERP documents per module (erp_documents): last sync, last document day, documents of 7 days,
                 drafts (New / Draft) of 14 days — a GR still New is out of the Stock List and the COGS
    so           Portal stock opname: outlets with sales and no count in SO_DAYS days (no SO akhir → no actual usage),
                 counts waiting for the Cost Control review
    waste        Portal waste: outlets that reported nothing yesterday, entries waiting for the review
    gr           Good Receipts: New for more than 2 days, corrected but not Finished, unusual lines of 7 days
    prices       Pricelist: materials used without a price (none at all / only the ESB HPP)
    recipes      menus sold in 7 days without a recipe card (left out of the system usage and the estimate)
    transfers    Transfer Stock not finished (requested, accepted, approved but not received)
Status: ok · warn (look at it) · high (the figures are wrong until fixed) · error (the check itself failed).
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app import database as db
from app.database import SCHEMA
from app.utils import Fresh, today

logger = logging.getLogger("integrated_portal_be")
P = "integration_portal"
CACHE_S = 300
SO_DAYS = 8
STALE_WARN_H, STALE_HIGH_H = 3, 24
MODULES = {
    "goods_receipt": "Goods Receipt", "purchase_invoice": "Purchase Invoice", "simple_purchase": "Pembelian outlet",
    "stock_opname": "Stock Opname ESB", "item_journal": "Item Journal", "goods_delivery": "Goods Delivery",
    "simple_manufacturing": "Produksi outlet", "purchase_return": "Purchase Return",
}
SKIP = ("Rejected", "Cancelled", "Canceled", "Void")


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


def _hours(at: Optional[datetime], now: datetime) -> Optional[float]:
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return round((now - at).total_seconds() / 3600, 1)


def stale_status(hours: Optional[float]) -> str:
    if hours is None or hours >= STALE_HIGH_H:
        return "high"
    return "warn" if hours >= STALE_WARN_H else "ok"


def worst(*statuses: str) -> str:
    order = ["ok", "warn", "high", "error"]
    return max(statuses or ("ok",), key=order.index)


def card(key: str, status: str, value: str, detail: str, source: str, link: Optional[str], **extra: Any) -> dict:
    return {"key": key, "status": status, "value": value, "detail": detail, "source": source, "link": link, **extra}


# ---------------------------------------------------------------- the checks

def esb_sales(now: datetime) -> dict:
    from app.routes.overview import freshness
    f = freshness()
    synced = datetime.fromisoformat(f["lastSyncedAt"]) if f.get("lastSyncedAt") else None
    h = _hours(synced, now)
    day = date.fromisoformat(f["dataTo"]) if f.get("dataTo") else None
    behind = (today() - day).days if day else None
    status = worst(stale_status(h), "high" if behind is None or behind > 1 else "ok")
    return card("esbSales", status, f"{h:g} jam" if h is not None else "—",
                f"Penjualan POS sampai {day.strftime('%d/%m/%Y') if day else '—'}; dipakai untuk penjualan, resep × menu terjual dan estimasi belanja",
                "ESB POS", "/sales", syncedAt=f.get("lastSyncedAt"), dataTo=f.get("dataTo"), hours=h)


def esb_docs(now: datetime) -> dict:
    d0 = today()
    rows = db.fetch(
        f"""SELECT module, max(synced_at) AS synced, max(doc_date) AS last_doc,
                   count(*) FILTER (WHERE doc_date >= %s) AS week,
                   count(*) FILTER (WHERE doc_date >= %s AND status_name IN ('New', 'Draft')) AS drafts
              FROM {SCHEMA}.erp_documents WHERE module = ANY(%s) AND deleted_at IS NULL AND doc_date >= %s GROUP BY 1""",
        (d0 - timedelta(days=6), d0 - timedelta(days=14), list(MODULES), d0 - timedelta(days=60)))
    by = {r["module"]: r for r in rows}
    items, statuses = [], []
    for m, name in MODULES.items():
        r = by.get(m) or {}
        h = _hours(r.get("synced"), now)
        s = stale_status(h) if m in ("goods_receipt", "stock_opname", "purchase_invoice") else ("ok" if h is None or h < STALE_HIGH_H else "warn")
        statuses.append(s)
        items.append({"module": m, "name": name, "status": s, "syncedAt": _iso(r.get("synced")), "hours": h, "lastDoc": _iso(r.get("last_doc")),
                      "week": int(r.get("week") or 0), "drafts": int(r.get("drafts") or 0)})
    st = worst(*statuses)
    late = [i["name"] for i in items if i["status"] != "ok"]
    return card("esbDocs", st, f"{len(MODULES) - len(late)}/{len(MODULES)}",
                "Semua modul dokumen ESB sinkron" if not late else f"Belum sinkron / tertinggal: {', '.join(late)}",
                "ESB ERP", "/cost-control/goods-receipt", items=items)


def so_counts() -> dict:
    from app import menu_costing as mc
    from app import test_branches
    d0 = today()
    active = {r["branch_code"] for r in db.fetch(
        f"""SELECT DISTINCT branch_code FROM {P}.agg_sales_daily WHERE sales_date BETWEEN %s AND %s AND tx_type = 'sales' AND subtotal > 0""",
        (d0 - timedelta(days=7), d0 - timedelta(days=1))) if r["branch_code"] and not test_branches.is_test(r["branch_code"])}
    counted = mc.portal_counts(d0, None, back=SO_DAYS, ahead=0)
    missing = sorted(active - set(counted))
    waiting = db.fetchrow("""SELECT count(*) FILTER (WHERE status = 'SUBMITTED') AS submitted,
                                    count(*) FILTER (WHERE status = 'RECOUNT_REQUESTED') AS recount
                               FROM kc_inventory.stock_count WHERE business_date >= %s""", (d0 - timedelta(days=30),)) or {}
    from app.pic_checkin import _names
    names = _names(missing[:200]) if missing else {}
    share = len(missing) / len(active) if active else 0
    status = "high" if share > 0.25 else "warn" if missing or (waiting.get("submitted") or 0) else "ok"
    return card("so", status, f"{len(active) - len(missing)}/{len(active)}",
                (f"{len(missing)} outlet berjualan tanpa SO portal dalam {SO_DAYS} hari: pemakaian aktual & stok berjalan outlet itu belum bisa dihitung"
                 if missing else f"Semua outlet yang berjualan punya SO portal dalam {SO_DAYS} hari")
                + f" · {int(waiting.get('submitted') or 0)} SO menunggu review, {int(waiting.get('recount') or 0)} diminta hitung ulang",
                "Portal SO", "/cost-control/reviews",
                items=[{"code": c, "name": names.get(c, c)} for c in missing[:50]], missing=len(missing), total=len(active),
                waiting=int(waiting.get("submitted") or 0), recount=int(waiting.get("recount") or 0))


def waste_reports() -> dict:
    from app import waste
    y = today() - timedelta(days=1)
    d = waste.daily(y, y, None)
    missing = d.get("missingLast") or []
    pending = db.fetchrow(f"SELECT count(*) AS n FROM {waste.T} WHERE status IN ('submitted', 'investigating') AND business_date >= %s",
                          (today() - timedelta(days=30),)) or {}
    total = len(d.get("branches") or [])
    status = "warn" if missing or (pending.get("n") or 0) > 0 else "ok"
    return card("waste", status, f"{total - len(missing)}/{total}",
                (f"{len(missing)} outlet belum lapor waste {y.strftime('%d/%m/%Y')}" if missing else f"Semua outlet lapor waste {y.strftime('%d/%m/%Y')}")
                + f" · {int(pending.get('n') or 0)} entri menunggu review (waste baru dihitung setelah disetujui)",
                "Portal Waste", "/cost-control/waste/reviews", items=missing[:50], missing=len(missing), pending=int(pending.get("n") or 0), total=total,
                day=y.isoformat())


def goods_receipts() -> dict:
    from app import goods_receipt as gr
    d0 = today()
    new = db.fetchrow(
        f"""SELECT count(*) AS n FROM {SCHEMA}.erp_documents WHERE module = 'goods_receipt' AND status_name = 'New' AND deleted_at IS NULL
              AND doc_date BETWEEN %s AND %s""", (d0 - timedelta(days=30), d0 - timedelta(days=3))) or {}
    try:
        open_ = db.fetchrow(
            f"""SELECT count(DISTINCT c.doc_num) AS n FROM {gr.T} c LEFT JOIN {gr.T_FIN} f ON f.doc_num = c.doc_num
                 WHERE c.{gr.ACTIVE.replace(' AND ', ' AND c.')} AND f.doc_num IS NULL""") or {}
    except Exception:  # noqa: BLE001 - before migration 077
        open_ = {}
    try:
        odd = db.fetchrow(f"""SELECT count(*) AS n FROM {P}.cc_alert WHERE kind IN ('doc', 'gr_short') AND created_at > now() - interval '7 days'""") or {}
    except Exception:  # noqa: BLE001 - before migration 098
        odd = {}
    n_new, n_open, n_odd = int(new.get("n") or 0), int(open_.get("n") or 0), int(odd.get("n") or 0)
    status = "warn" if n_new or n_open or n_odd else "ok"
    return card("gr", status, str(n_new + n_open),
                f"{n_new} GR masih New > 2 hari (belum masuk Stock List & COGS) · {n_open} GR terkoreksi belum Selesai · {n_odd} baris tidak biasa dalam 7 hari",
                "ESB GR + koreksi Portal", "/cost-control/goods-receipt", new=n_new, open=n_open, unusual=n_odd)


def prices() -> dict:
    from app import menu_costing as mc
    u = mc.unpriced_items()
    none, esb = u["counts"]["none"], u["counts"]["esb"]
    status = "high" if none else "warn" if esb else "ok"
    return card("prices", status, str(none + esb),
                f"{none} bahan dipakai tanpa harga sama sekali (HPP menu & nilai waste kosong) · {esb} hanya memakai HPP ESB (belum dicek Cost Control)",
                "Portal Pricelist", "/cost-control/pricelist",
                items=[{"code": x["code"], "name": x["name"], "level": x["level"]} for x in u["data"][:50]], none=none, esb=esb)


def recipes() -> dict:
    from app import menu_costing as mc
    rows = [m for m in mc.menus(7) if m["qty"] > 0 and "menu" in m["kinds"]]
    without = [m for m in rows if not m["recipe"]]
    qty = sum(m["qty"] for m in rows)
    miss = sum(m["qty"] for m in without)
    share = miss / qty if qty else 0
    status = "high" if share > 0.05 else "warn" if without else "ok"
    return card("recipes", status, f"{share * 100:.1f}%",
                f"{len(without)} menu terjual 7 hari tanpa kartu resep ({share * 100:.1f}% porsi): bahannya tidak masuk pemakaian sistem dan estimasi belanja",
                "ESB POS + kartu resep Portal", "/cost-control/sales-calculator",
                items=[{"id": m["id"], "name": m["name"], "qty": m["qty"]} for m in sorted(without, key=lambda x: -x["qty"])[:50]], menus=len(without),
                share=round(share, 4))


def transfers() -> dict:
    from app import stock_transfer as st
    rows = db.fetch(f"""SELECT status, count(*) AS n, min(created_at) AS oldest FROM {st.T}
                          WHERE status IN ('submitted', 'accepted', 'approved') GROUP BY 1""")
    by = {r["status"]: int(r["n"]) for r in rows}
    n = sum(by.values())
    status = "warn" if by.get("accepted") or by.get("approved") else "ok"
    return card("transfers", status, str(n),
                f"{by.get('submitted', 0)} diajukan · {by.get('accepted', 0)} menunggu Cost Control · {by.get('approved', 0)} dalam perjalanan "
                "(barang yang belum diterima tidak ada di stok kedua outlet)",
                "Portal Transfer Stock", "/cost-control/stock-transfer", byStatus=by)


CHECKS: dict[str, Callable[..., dict]] = {
    "esbSales": esb_sales, "esbDocs": esb_docs, "so": lambda now: so_counts(), "waste": lambda now: waste_reports(),
    "gr": lambda now: goods_receipts(), "prices": lambda now: prices(), "recipes": lambda now: recipes(),
    "transfers": lambda now: transfers(),
}


def _compute() -> dict:
    now = datetime.now(timezone.utc)
    cards = []
    for key, fn in CHECKS.items():
        try:
            cards.append(fn(now))
        except Exception as exc:  # noqa: BLE001 - one source failing leaves the others
            logger.exception("data health: %s failed", key)
            cards.append(card(key, "error", "—", f"Pemeriksaan gagal: {str(exc)[:160]}", "", None))
    counts = {s: sum(1 for c in cards if c["status"] == s) for s in ("ok", "warn", "high", "error")}
    return {"at": now.isoformat(), "cards": cards, "counts": counts}


_all = Fresh(_compute, CACHE_S)


def health(keys: Optional[list[str]] = None) -> dict:
    data = _all.get()
    if not keys:
        return data
    return {**data, "cards": [c for c in data["cards"] if c["key"] in set(keys)]}


# ---------------------------------------------------------------- the bell (app/alerts.py)

def health_alerts(day: date, hour: int) -> list[dict]:
    """Daily alerts from the checks (one per kind and day; the dedup keeps a rescan harmless). From 09:00 WIB, so the
    night syncs and the morning reports are in."""
    if hour < 9:
        return []
    out = []
    for c in health()["cards"]:
        if c["status"] not in ("warn", "high") or c["key"] == "gr":  # the GRs have their own alerts
            continue
        out.append({"dedup": f"health:{c['key']}:{day}", "kind": f"health_{c['key']}", "severity": "high" if c["status"] == "high" else "warn",
                    "title": TITLES.get(c["key"], c["key"]), "body": c["detail"][:500], "link": c["link"]})
    return out


TITLES = {
    "esbSales": "Penjualan ESB belum sinkron", "esbDocs": "Dokumen ESB tertinggal", "so": "Outlet tanpa SO portal",
    "waste": "Outlet belum lapor waste", "prices": "Bahan tanpa harga", "recipes": "Menu tanpa kartu resep",
    "transfers": "Transfer Stock belum selesai",
}
