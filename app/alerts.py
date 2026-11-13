"""The notification bell of the portal (Cost Control, 2026-11-13; migration 098).

Not one alert per goods receipt (~90 a day: nobody reads them after a week, the important ones with them) — only what
needs a look, scanned in the background every SCAN_S seconds:

    doc        a line of a GR or another ESB document moving the stock of an outlet with a quantity far from usual
               (app/doc_checks.py: 20× or more, or that much less), not corrected yet — per line
    gr_short   a GR line received below SHORT of its purchase order (same item and unit) — per line
    gr_new     daily: the GRs still New after NEW_DAYS days (the Stock List leaves them out until they are authorized)
    gr_open    daily: the GRs Cost Control corrected but did not mark Finished after NEW_DAYS days (the corrections
               do not count until then)
    gr_daily   daily from 06:00 WIB: the GRs of yesterday and how many of them needed a look

`dedup` makes a scan harmless to repeat (several workers, a restart). Who opens Good Receipt or COGS & Usage sees the
bell; each user marks alerts read for themselves. Alerts older than KEEP_DAYS are deleted.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import database as db
from app.database import SCHEMA

logger = logging.getLogger("integrated_portal_be")
P = "integration_portal"
T, T_READ = f"{P}.cc_alert", f"{P}.cc_alert_read"
PAGES = ("cc.goods-receipt", "cc.cogs", "cc.info", "cc.purchase-plan", "cc.ratio", "cc.sales-calc", "cc.reviews", "cc.waste-review")
SCAN_S = 1800
FIRST_SCAN_S = 90
LOOK_BACK = 3        # days of documents scanned for the per-line alerts
SHORT = 0.5          # a GR line below half of its PO
NEW_DAYS = 2
KEEP_DAYS = 60
SHOW_DAYS = 30
LOCK = 980_098       # pg advisory lock: one worker scans at a time
SKIP = ("New", "Draft", "Rejected", "Cancelled", "Canceled", "Void")
KIND = {"received": "GR", "outletBuy": "Pembelian outlet", "produced": "Hasil produksi", "consumed": "Bahan produksi",
        "delivered": "Pengiriman barang", "returned": "Retur"}


def _num(v: float) -> str:
    s = f"{v:,.3f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _gr_link(doc: str, day: Optional[str]) -> str:
    from urllib.parse import quote
    return f"/cost-control/goods-receipt?doc={quote(doc)}" + (f"&date={day}" if day else "")


def _add(conn, a: dict) -> int:
    row = conn.execute(
        f"""INSERT INTO {T} (dedup, kind, severity, title, body, link, branch_code, doc_num)
            VALUES (%(dedup)s, %(kind)s, %(severity)s, %(title)s, %(body)s, %(link)s, %(branch)s, %(doc)s)
            ON CONFLICT (dedup) DO NOTHING RETURNING id""",
        {"body": None, "link": None, "branch": None, "doc": None, **a}).fetchone()
    return 1 if row else 0


def unusual_docs(today: date) -> list[dict]:
    """The lines of app/doc_checks.py not corrected yet, of the last LOOK_BACK days."""
    from app import doc_checks
    out = []
    for x in doc_checks.check(today - timedelta(days=LOOK_BACK), today, None)["data"]:
        if not x["unusual"] or x["fix"]:
            continue
        what = KIND.get(x["kind"], x["kind"])
        out.append({
            "dedup": f"doc:{x['module']}:{x['docNum']}:{x['lineType']}:{x['code']}:{x['uom']}", "kind": "doc",
            "severity": "high" if x["severe"] else "warn",
            "title": f"{what} tidak wajar: {x['name']}" + (" (salah satuan?)" if x["severe"] else ""),
            "body": f"{x['branchName']} · {x['docNum']} · {_num(x['qty'])} {x['uom']}, biasanya {_num(x['usual'])} {x['unit']}"
                    + (f" (×{_num(x['factor'])})" if x["factor"] else "")
                    + (f" · dampak ± Rp {_num(round(x['impact']))}" if x["impact"] else ""),
            "link": _gr_link(x["docNum"], x["docDate"]) if x["ownPage"] else "/cost-control",
            "branch": x["branch"], "doc": x["docNum"]})
    return out


def short_receipts(today: date) -> list[dict]:
    rows = db.fetch(
        f"""WITH g AS (SELECT d.doc_num, d.doc_date, d.ref_num, b.branch_code, coalesce(b.branch_name, d.branch_name) AS branch_name,
                              l.product_code, max(l.product_name) AS name, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty) AS qty
                         FROM {SCHEMA}.erp_documents d
                         JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
                         LEFT JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
                        WHERE d.module = 'goods_receipt' AND d.doc_date BETWEEN %(a)s AND %(b)s AND d.deleted_at IS NULL
                          AND coalesce(d.status_name, '') <> ALL(%(skip)s) AND l.product_code IS NOT NULL
                        GROUP BY 1, 2, 3, 4, 5, 6, 8),
                 p AS (SELECT l.doc_num, l.product_code, upper(coalesce(l.uom_name, '')) AS uom, sum(l.qty) AS qty
                         FROM {SCHEMA}.erp_document_lines l
                        WHERE l.module = 'purchase_order' AND l.line_type = 'detail'
                          AND l.doc_num IN (SELECT DISTINCT ref_num FROM g WHERE ref_num IS NOT NULL)
                        GROUP BY 1, 2, 3)
            SELECT g.*, p.qty AS po_qty FROM g JOIN p ON p.doc_num = g.ref_num AND p.product_code = g.product_code AND p.uom = g.uom
             WHERE p.qty > 0 AND g.qty < p.qty * %(short)s""",
        {"a": today - timedelta(days=LOOK_BACK), "b": today, "skip": list(SKIP), "short": SHORT})
    return [{
        "dedup": f"gr_short:{r['doc_num']}:{r['product_code']}:{r['uom']}", "kind": "gr_short", "severity": "warn",
        "title": f"GR jauh di bawah PO: {r['name'] or r['product_code']}",
        "body": f"{r['branch_name']} · {r['doc_num']} · diterima {_num(float(r['qty']))} dari {_num(float(r['po_qty']))} {r['uom']} (PO {r['ref_num']})",
        "link": _gr_link(r["doc_num"], r["doc_date"].isoformat()), "branch": r["branch_code"], "doc": r["doc_num"]} for r in rows]


def daily(today: date, hour: int) -> list[dict]:
    from app import goods_receipt as gr
    out = []
    new = db.fetch(
        f"""SELECT coalesce(b.branch_name, d.branch_name) AS name, count(*) AS n
              FROM {SCHEMA}.erp_documents d LEFT JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
             WHERE d.module = 'goods_receipt' AND d.status_name = 'New' AND d.deleted_at IS NULL
               AND d.doc_date BETWEEN %s AND %s GROUP BY 1 ORDER BY 2 DESC, 1""",
        (today - timedelta(days=30), today - timedelta(days=NEW_DAYS + 1)))
    if new:
        n = sum(int(r["n"]) for r in new)
        top = ", ".join(f"{r['name']} ({r['n']})" for r in new[:5])
        out.append({"dedup": f"gr_new:{today}", "kind": "gr_new", "severity": "warn",
                    "title": f"{n} GR masih New lebih dari {NEW_DAYS} hari",
                    "body": f"Belum masuk Stock List sampai di-authorize di ESB · {len(new)} outlet: {top}"
                            + (" …" if len(new) > 5 else ""),
                    "link": "/cost-control/goods-receipt"})
    try:
        open_ = db.fetchrow(
            f"""SELECT count(DISTINCT c.doc_num) AS n FROM {gr.T} c LEFT JOIN {gr.T_FIN} f ON f.doc_num = c.doc_num
                 WHERE c.{gr.ACTIVE.replace(' AND ', ' AND c.')} AND f.doc_num IS NULL
                   AND c.created_at < now() - make_interval(days => %s)""", (NEW_DAYS,))
    except Exception:  # noqa: BLE001 - before migration 077
        open_ = None
    if open_ and open_["n"]:
        out.append({"dedup": f"gr_open:{today}", "kind": "gr_open", "severity": "warn",
                    "title": f"{open_['n']} GR terkoreksi belum ditandai Selesai",
                    "body": f"Koreksinya belum dihitung di Stock List dan COGS sampai GR ditandai Selesai (lebih dari {NEW_DAYS} hari)",
                    "link": "/cost-control/goods-receipt"})
    if hour >= 6:
        y = today - timedelta(days=1)
        s = db.fetchrow(
            f"""SELECT count(*) AS docs, count(DISTINCT d.branch_id) AS outlets
                  FROM {SCHEMA}.erp_documents d WHERE d.module = 'goods_receipt' AND d.doc_date = %s AND d.deleted_at IS NULL
                   AND coalesce(d.status_name, '') <> ALL(%s)""", (y, ["Rejected", "Cancelled", "Canceled", "Void"]))
        looked = db.fetchrow(f"""SELECT count(DISTINCT doc_num) AS n FROM {T} WHERE kind IN ('doc', 'gr_short')
                                   AND created_at > now() - interval '1 day'""")
        if s and s["docs"]:
            out.append({"dedup": f"gr_daily:{y}", "kind": "gr_daily", "severity": "info",
                        "title": f"Ringkasan GR {y.strftime('%d/%m/%Y')}",
                        "body": f"{s['docs']} GR di {s['outlets']} outlet · {looked['n'] if looked else 0} dokumen perlu dicek dalam 24 jam terakhir",
                        "link": f"/cost-control/goods-receipt?date={y.isoformat()}"})
    return out


def _health(today: date, hour: int) -> list[dict]:
    """The daily alerts of the Pusat Informasi (app/data_health.py): ESB not synced, outlets without SO or waste report,
    materials without a price, menus without a card, Transfer Stock not finished (2026-11-13)."""
    from app import data_health
    return data_health.health_alerts(today, hour)


def scan(now: Optional[datetime] = None) -> dict:
    """One scan (one worker at a time): the new alerts are stored, the old ones deleted. {added, checked}."""
    from app.config import get_settings
    now = now or datetime.now(ZoneInfo(get_settings().timezone))
    today = now.date()
    found: list[dict] = []
    for name, step in (("doc", lambda: unusual_docs(today)), ("short", lambda: short_receipts(today)),
                       ("daily", lambda: daily(today, now.hour)), ("health", lambda: _health(today, now.hour))):
        try:
            found += step()
        except Exception:  # noqa: BLE001 - one rule failing leaves the others
            logger.exception("alerts: %s failed", name)
    added = 0
    with db.transaction() as conn:
        got = conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK,)).fetchone()
        if not (got["ok"] if isinstance(got, dict) else got[0]):
            return {"added": 0, "checked": len(found), "skipped": True}
        for a in found:
            added += _add(conn, a)
        conn.execute(f"DELETE FROM {T} WHERE created_at < now() - make_interval(days => %s)", (KEEP_DAYS,))
    return {"added": added, "checked": len(found)}


def _loop() -> None:
    time.sleep(FIRST_SCAN_S)
    while True:
        try:
            out = scan()
            if out.get("added"):
                logger.info("alerts: %s new", out["added"])
        except Exception:  # noqa: BLE001 - the bell is extra: never stop the worker
            logger.exception("alerts: scan failed")
        time.sleep(SCAN_S)


def start() -> None:
    """The start of a worker: the background scan."""
    threading.Thread(target=_loop, name="cc-alerts", daemon=True).start()


# ---------------------------------------------------------------- the bell

def _view(r: dict) -> dict:
    return {"id": r["id"], "kind": r["kind"], "severity": r["severity"], "title": r["title"], "body": r["body"],
            "link": r["link"], "branch": r["branch_code"], "doc": r["doc_num"], "at": r["created_at"].isoformat(),
            "read": r["read_at"] is not None}


def for_user(user: dict, limit: int = 50) -> dict:
    uid = str(user["id"])
    rows = db.fetch(
        f"""SELECT a.*, r.read_at FROM {T} a LEFT JOIN {T_READ} r ON r.alert_id = a.id AND r.user_id = %s
             WHERE a.created_at > now() - make_interval(days => %s) ORDER BY a.created_at DESC, a.id DESC LIMIT %s""",
        (uid, SHOW_DAYS, limit))
    unread = db.fetchrow(
        f"""SELECT count(*) AS n FROM {T} a WHERE a.created_at > now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM {T_READ} r WHERE r.alert_id = a.id AND r.user_id = %s)""", (SHOW_DAYS, uid))
    return {"unread": int(unread["n"]) if unread else 0, "data": [_view(r) for r in rows]}


def mark_read(user: dict, ids: Optional[list[int]], everything: bool) -> int:
    if not everything and not ids:
        return 0
    with db.transaction() as conn:
        cur = conn.execute(
            f"""INSERT INTO {T_READ} (alert_id, user_id)
                SELECT id, %s FROM {T} WHERE created_at > now() - make_interval(days => %s) AND (%s OR id = ANY(%s))
                ON CONFLICT DO NOTHING""", (str(user["id"]), SHOW_DAYS, everything, ids or []))
        return cur.rowcount or 0
