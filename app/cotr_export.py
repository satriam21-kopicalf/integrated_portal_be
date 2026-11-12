"""The month of Calf On The Road in one Excel file (recommendation #12, 2026-11-12), for finance and the owners: Product
(the stock of every pool per day and menu), Report Armada (per driver per day), Batch Codes, Waste & Cupping, Fleet
Issues and the Reconciliation workbook vs ESB. One pool or every pool."""
import io
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import database as db
from app import cotr_ledger
from app.cotr import P
from app.xlsx_stream import StreamingXlsxWriter

KIND = {"transfer": "Transfer", "arrived": "Masuk", "sold": "Terjual", "waste": "Waste", "cupping": "Cupping"}
SOURCE = {"sheet": "Workbook", "esb": "ESB", "app": "Aplikasi", "opening": "Stok awal", "mixed": "Campuran"}


def month_range(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def monthly_workbook(month: date, pool: Optional[str]) -> bytes:
    start, end = month_range(month)
    end = min(end, date.today())
    names = {r["code"]: r["name"] for r in db.fetch(f"SELECT code, name FROM {P}.cotr_pool")}
    pools = [pool] if pool else [r["code"] for r in db.fetch(f"SELECT DISTINCT pool_code AS code FROM {P}.cotr_pool_count ORDER BY 1")]
    stamp = f"Diunduh {datetime.now(ZoneInfo('Asia/Jakarta')):%d/%m/%Y %H:%M} WIB"
    scope = names.get(pool, pool) if pool else "Semua pool / HUB"
    title = lambda what: [[f"{what} · {scope} · {start:%m/%Y}"], [stamp], []]  # noqa: E731
    buf = io.BytesIO()
    with StreamingXlsxWriter(buf) as x:
        # Product: per pool, day and menu
        sh = x.add_sheet("Product", widths=[12, 24, 22, 10, 10, 10, 10, 10, 10, 12, 10, 10, 12],
                         header=["Tanggal", "Pool / HUB", "Menu", "Awal", "Transfer", "Masuk", "Terjual", "Waste", "Cupping", "Perhitungan",
                                 "Aktual", "Balance", "Sumber"], preamble=title("Stok Produk Pool"))
        for p in pools:
            r = cotr_ledger.run(p, start, end)
            menus = {m["code"]: m["name"] for m in r["menus"]}
            for d in r["days"]:
                if d["source"] == "opening":
                    continue
                for c, v in d["rows"].items():
                    sh.write_rows([[d["day"], names.get(p, p), menus.get(c, c), v.get("opening"), v.get("transfer"), v.get("arrived"), v.get("sold"),
                                    v.get("waste"), v.get("cupping"), v.get("calculated"), v.get("actual"), v.get("balance"), SOURCE.get(d["source"], d["source"])]])
        # Report Armada: per driver and day
        sh = x.add_sheet("Report Armada", widths=[12, 24, 28, 10, 10, 10, 10, 10, 14, 14, 14, 14, 10],
                         header=["Tanggal", "Pool / HUB", "Driver", "Bawa (B)", "Sisa (S)", "Cupping (C)", "Waste (W)", "Terjual (T)", "Penjualan (Rp)",
                                 "Tunai (Rp)", "QRIS (Rp)", "Operasional (Rp)", "Sumber"], preamble=title("Report Armada"))
        for p in pools:
            for d in cotr_ledger.report_armada(p, start, end)["drivers"]:
                for day in d["days"]:
                    lines = day["lines"].values()
                    tot = lambda k: sum(int(v.get(k) or 0) for v in lines)  # noqa: E731
                    sh.write_rows([[day["day"], names.get(d["pool"], d["pool"]), d["name"], tot("b"), tot("s"), tot("c"), tot("w"), tot("t"),
                                    round(day["total"]), round(day.get("cash") or 0), round(day.get("qris") or 0), round(day.get("operational") or 0),
                                    "Workbook" if day["status"] == "sheet" else "Aplikasi"]])
        # Batch codes counted at the pools
        sh = x.add_sheet("Kode Batch", widths=[22, 12, 10, 12, 12, 12, 12, 12, 40],
                         header=["Menu", "Kode batch", "Batch ke", "Produksi", "Kedaluwarsa", "Pertama dihitung", "Terakhir dihitung", "Sisa", "Per pool"],
                         preamble=title("Kode Batch"))
        for b in cotr_ledger.batch_codes(pool, start, end)["batches"]:
            sh.write_rows([[b["menuName"], b["batchNo"], b.get("sequence"), b.get("producedOn"), b.get("expiresOn"), b.get("firstCounted"),
                            b.get("lastCounted"), b.get("left"), ", ".join(f"{q['poolName']}: {q['qty']}" for q in b.get("pools", []))]])
        # Waste & cupping of the pools
        sh = x.add_sheet("Waste & Cupping", widths=[12, 24, 12, 28, 10, 8, 40],
                         header=["Tanggal", "Pool / HUB", "Jenis", "Item", "Qty", "Satuan", "Keterangan"], preamble=title("Waste & Cupping Pool"))
        for r in db.fetch(f"""SELECT day, pool_code, kind, item, qty, unit, reason FROM {P}.cotr_waste
                               WHERE day BETWEEN %s AND %s AND (%s::text IS NULL OR pool_code = %s) ORDER BY day, pool_code""", (start, end, pool, pool)):
            sh.write_rows([[r["day"].isoformat(), names.get(r["pool_code"], r["pool_code"]), r["kind"], r["item"], float(r["qty"] or 0), r["unit"], r["reason"]]])
        # Fleet issues
        sh = x.add_sheet("Kendala Armada", widths=[17, 12, 24, 14, 10, 34, 14, 22, 40],
                         header=["Dilaporkan", "Armada", "Driver", "Kategori", "Tingkat", "Judul", "Status", "Ditangani", "Penyelesaian"],
                         preamble=title("Kendala Armada"))
        for r in db.fetch(f"""SELECT i.created_at, i.fleet_code, d.name AS driver, i.category, i.severity, i.title, i.status, i.handler, i.resolution_note
                                FROM {P}.cotr_fleet_issue i LEFT JOIN {P}.cotr_driver d ON d.code = i.driver_code
                               WHERE (i.created_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s
                                 AND (%s::text IS NULL OR d.pool_code = %s) ORDER BY i.created_at""", (start, end, pool, pool)):
            sh.write_rows([[r["created_at"].astimezone(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M"), r["fleet_code"], r["driver"], r["category"],
                            r["severity"], r["title"], r["status"], r["handler"], r["resolution_note"]]])
        # Reconciliation workbook vs ESB (the differences)
        sh = x.add_sheet("Rekonsiliasi", widths=[12, 24, 12, 22, 12, 12, 10, 10, 14],
                         header=["Tanggal", "Pool / HUB", "Status", "Menu", "Gerakan", "Workbook", "ESB", "Aplikasi", "Workbook − ESB"],
                         preamble=title("Rekonsiliasi Workbook vs ESB (selisih)"))
        for d in cotr_ledger.reconcile(pool, start, end)["days"]:
            for line in d["lines"]:
                if line["diff"] or d["status"] in ("sheet", "esb"):
                    sh.write_rows([[d["day"], d["poolName"], d["status"], line["name"], KIND.get(line["kind"], line["kind"]), line["sheet"], line["esb"],
                                    line["app"], line["diff"]]])
    return buf.getvalue()
