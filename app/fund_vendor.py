"""Vendor payments in bulk (migration 150, 2026-11-25): the Warehouse and Central Kitchen send ~100 invoices a day.

  import    an Excel with the columns of the Google Form recap (or the template) → one draft per row; a row already sent,
            already a draft or already paid through the Google Form (fund_external) is refused with the reason
  attach    files matched to the drafts by the invoice number in the file name
  send      every complete draft at once; what is missing on the others is listed
  approve   several requests at once; one with the same account and amount as another within 30 days is left to be
            opened and checked by itself
  pay       several approved requests of ONE paying PT with one transfer date and the proof of the bank batch
Every request keeps its own history: in bulk is only how the same steps are taken (pengajuan.change_status).
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Optional

from app import database as db
from app import fund_units
from app import pengajuan as pg
from app.pic_checkin import branch_options

P = "integration_portal"
MAX_ROWS = 500
MAX_BULK = 200
COLUMNS = [("vendor", "NAMA VENDOR"), ("term", "Keperluan Dana (COD / TOP / CBD)"), ("purpose", "Deskripsi dan Detail Keperluan"),
           ("invoiceNo", "Nomor Invoice/Nota/Kwitansi"), ("invoiceDate", "Tanggal Invoice"), ("due", "Tanggal Jatuh Tempo"),
           ("outstanding", "Total Outstanding Invoice"), ("amount", "Total Dana Diajukan"), ("account", "Nomor rekening"),
           ("payee", "Nama Rekening"), ("bank", "Bank Tujuan"), ("department", "Departemen")]
STARTS = {"namavendor": "vendor", "vendor": "vendor", "keperluandana": "term", "termin": "term", "deskripsi": "purpose", "nomorinvoice": "invoiceNo",
          "tanggalinvoice": "invoiceDate", "tanggaljatuhtempo": "due", "totaloutstanding": "outstanding", "totaldana": "amount",
          "nomorrekening": "account", "namarekening": "payee", "banktujuan": "bank", "departemen": "department"}
NEED = ("vendor", "term", "purpose", "invoiceNo", "invoiceDate", "due", "amount", "account", "bank")
CENT = Decimal("0.01")


def _key(v: Any) -> str:
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def _day(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(v or "").strip()[:10], f).date()
        except ValueError:
            continue
    return None


def _rp(v: Any) -> Optional[Decimal]:
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return Decimal(str(v)).quantize(CENT)
    s = str(v or "").replace("Rp", "").replace(" ", "")
    if not s:
        return None
    try:
        return Decimal(s.replace(".", "").replace(",", "."))  # 1.250.000,50
    except InvalidOperation:
        return None


def unit_of_department(v: Any) -> Optional[str]:
    k = _key(v)
    if "roast" in k:
        return "ROASTERY"
    if "warehouse" in k or "gudang" in k or k.startswith("wh"):
        return "WAREHOUSE"
    if "centralkitchen" in k or k.startswith("ck"):
        return "CK"
    return None


def parse(data: bytes) -> list[dict]:
    """The rows of the first sheet: the cells of the known columns, the row number and what is wrong with it. Pure (tested)."""
    import openpyxl
    try:
        ws = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True).worksheets[0]
    except Exception:  # noqa: BLE001
        raise pg.PengajuanError("File bukan Excel (.xlsx) yang dapat dibaca") from None
    head: dict[int, str] = {}
    out: list[dict] = []
    for n, cells in enumerate(ws.iter_rows(values_only=True), start=1):
        if not head:
            found: dict[int, str] = {}
            for i, c in enumerate(cells):
                k = next((f for s, f in STARTS.items() if _key(c).startswith(s)), None)
                if k and k not in found.values():
                    found[i] = k
            if len(found) >= 6:
                missing = [label for f, label in COLUMNS if f in NEED and f not in found.values()]
                if missing:
                    raise pg.PengajuanError("Kolom tidak ditemukan: " + ", ".join(missing))
                head = found
            elif n > 15:
                break
            continue
        raw = {f: cells[i] if i < len(cells) else None for i, f in head.items()}
        if not str(raw.get("vendor") or "").strip() and raw.get("amount") in (None, ""):
            continue
        out.append(clean(n, raw))
        if len(out) > MAX_ROWS:
            raise pg.PengajuanError(f"Maksimal {MAX_ROWS} baris per file")
    if not head:
        raise pg.PengajuanError("Baris judul tidak ditemukan: pakai template atau recap Google Form")
    if not out:
        raise pg.PengajuanError("Tidak ada baris yang diisi")
    return out


def clean(n: int, raw: dict) -> dict:
    def text(f: str, size: int) -> str:
        v = raw.get(f)
        return " ".join(str(v if v is not None else "").split())[:size]
    problems: list[str] = []
    warns: list[str] = []
    acc = raw.get("account")
    if isinstance(acc, (int, float)) and not isinstance(acc, bool):
        account = f"{acc:.0f}"
        if len(account) > 15:
            problems.append("nomor rekening terbaca sebagai angka dan digitnya hilang: ubah kolomnya menjadi teks")
        else:
            warns.append("nomor rekening terbaca sebagai angka: cek nol di depan")
    else:
        account = text("account", 40)
    term = text("term", 20).upper()[:3]
    bank = text("bank", 60)
    va = "VA" in bank.upper().split()
    r = {"row": n, "vendor": text("vendor", 120), "term": term, "purpose": text("purpose", 1000), "invoiceNo": text("invoiceNo", 80),
         "invoiceDate": _day(raw.get("invoiceDate")), "due": _day(raw.get("due")), "amount": _rp(raw.get("amount")),
         "outstanding": _rp(raw.get("outstanding")), "account": account, "payee": text("payee", 80), "bank": bank, "va": va,
         "unit": unit_of_department(raw.get("department"))}
    for f, what in (("vendor", "nama vendor"), ("purpose", "deskripsi"), ("invoiceNo", "nomor invoice"), ("account", "nomor rekening"), ("bank", "bank tujuan")):
        if not r[f]:
            problems.append(f"{what} kosong")
    if not va and not r["payee"]:
        problems.append("nama rekening kosong")
    if term not in pg.TERMS:
        problems.append("Keperluan Dana harus COD, TOP atau CBD")
    if not r["invoiceDate"]:
        problems.append("tanggal invoice tidak terbaca")
    if not r["due"]:
        problems.append("tanggal jatuh tempo tidak terbaca")
    if r["invoiceDate"] and r["due"] and r["due"] < r["invoiceDate"]:
        problems.append("jatuh tempo sebelum tanggal invoice")
    if r["amount"] is None or r["amount"] <= 0:
        problems.append("total dana diajukan tidak terbaca")
    elif r["outstanding"] is not None and r["amount"] > r["outstanding"]:
        problems.append("dana diajukan melebihi outstanding")
    if r["due"] and r["due"] < pg._now().date():
        warns.append("jatuh tempo sudah lewat")
    return {**r, "problems": problems, "warnings": warns}


def _sig(account: Any, invoice: Any, amount: Any) -> tuple[str, str, Decimal]:
    return pg._digits(account), pg._digits(invoice), Decimal(str(amount or 0)).quantize(CENT)


def known() -> dict[tuple, str]:
    """{(account, invoice, amount): where} of the live requests, the drafts and what the Google Form already paid."""
    out: dict[tuple, str] = {}
    for r in db.fetch(f"SELECT account, invoice, amount, label FROM {P}.fund_external"):
        out[(r["account"], r["invoice"], Decimal(r["amount"]).quantize(CENT))] = r["label"]
    for r in db.fetch(f"""SELECT coalesce(number, 'draf ' || coalesce(full_name, username)) AS n, payee_account, invoice_no, requested_amount
                            FROM {P}.pengajuan WHERE kind = 'VENDOR' AND status NOT IN ('REJECTED', 'CANCELLED') AND requested_amount IS NOT NULL"""):
        out[_sig(r["payee_account"], r["invoice_no"], r["requested_amount"])] = r["n"]
    return out


def check(user: dict, data: bytes, unit: Optional[str]) -> list[dict]:
    rows = parse(data)
    mine = [c for c in fund_units.requests_for(user, branch_options(user)) if (fund_units.get(c) or {}).get("vendor")]
    have: dict[tuple, str] = known()
    seen: dict[tuple, int] = {}
    for r in rows:
        r["unit"] = r["unit"] or unit
        if r["unit"] is None:
            r["problems"].append("pilih unit (kolom Departemen kosong atau tidak dikenal)")
        elif r["unit"] not in mine:
            r["problems"].append("unit di luar akses akun Anda")
        s = _sig(r["account"], r["invoiceNo"], r["amount"])
        if s[1]:  # "-" is no invoice number: never a duplicate by itself
            if s in have:
                r["problems"].append(f"sudah ada: {have[s]}")
            elif s in seen:
                r["problems"].append(f"sama dengan baris {seen[s]}")
            seen.setdefault(s, r["row"])
    return rows


def view_row(r: dict) -> dict:
    return {**r, "amount": float(r["amount"]) if r["amount"] is not None else None,
            "outstanding": float(r["outstanding"]) if r["outstanding"] is not None else None,
            "invoiceDate": r["invoiceDate"].isoformat() if r["invoiceDate"] else None, "due": r["due"].isoformat() if r["due"] else None}


def import_rows(user: dict, data: bytes, unit: Optional[str], apply: bool) -> dict:
    rows = check(user, data, unit)
    good = [r for r in rows if not r["problems"]]
    made = 0
    if apply:
        me = pg._profile(user)
        with db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(980150)")  # the same file sent twice at once makes one set of drafts
            have = known()
            for r in good:
                s = _sig(r["account"], r["invoiceNo"], r["amount"])
                if s[1] and s in have:
                    continue
                pid = conn.execute(
                    f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, kind, baristas, email, department, job_title, requester_name,
                            vendor_name, payment_term, purpose, invoice_no, invoice_date, needed_on, outstanding_amount, requested_amount,
                            payee_account, payee_name, payee_bank, payee_type, va_provider)
                        VALUES (%s, %s, %s, 'VENDOR', '[]'::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                    (user["id"], user["username"], user.get("full_name"), me["email"], me["department"], me["jobTitle"], me["name"], r["vendor"],
                     r["term"], r["purpose"], r["invoiceNo"], r["invoiceDate"], r["due"], r["outstanding"], r["amount"], r["account"],
                     r["payee"] or (r["bank"] if r["va"] else None), r["bank"], "VA" if r["va"] else "BANK", r["bank"] if r["va"] else None)).fetchone()["id"]
                fund_units.stamp(conn, pid, r["unit"])
                made += 1
    return {"rows": [view_row(r) for r in rows], "ready": len(good), "created": made,
            "total": float(sum((r["amount"] for r in good), Decimal(0)))}


def drafts(user: dict) -> list[dict]:
    rows = db.fetch(f"""SELECT {pg.COLS} FROM {P}.pengajuan p WHERE p.user_id::text = %s AND p.kind = 'VENDOR' AND p.status = 'DRAFT'
                        ORDER BY p.created_at DESC LIMIT 500""", (str(user["id"]),))
    files: dict[str, int] = {}
    if rows:
        for f in db.fetch(f"""SELECT pengajuan_id::text AS p, count(*)::int AS n FROM {P}.pengajuan_file
                              WHERE kind = 'OTHER' AND pengajuan_id::text = ANY(%s) GROUP BY 1""", ([r["id"] for r in rows],)):
            files[f["p"]] = f["n"]
    return [{"id": r["id"], "unit": r["unit"], "vendor": r["vendor_name"], "invoiceNo": r["invoice_no"], "term": r["payment_term"],
             "due": pg._iso(r["needed_on"]), "amount": pg._num(r["requested_amount"]), "account": r["payee_account"], "files": files.get(r["id"], 0),
             "missing": pg.missing_vendor(r, [{"kind": "OTHER"}] * files.get(r["id"], 0))} for r in rows]


def match_file(name: str, invoices: list[tuple[str, str]]) -> list[str]:
    """The drafts whose invoice number is in the file name; the longest match wins (INV-1 is not INV-12). Pure (tested)."""
    key = pg._digits(name.rsplit(".", 1)[0])
    hits = [(pg._digits(no), pid) for pid, no in invoices if len(pg._digits(no)) >= 4 and pg._digits(no) in key]
    best = max((len(k) for k, _ in hits), default=0)
    return [pid for k, pid in hits if len(k) == best]


def attach(user: dict, data: bytes, name: str, ids: Optional[list[str]] = None) -> dict:
    mine = drafts(user)
    own = {d["id"] for d in mine}
    to = [i for i in ids if i in own] if ids else match_file(name, [(d["id"], d["invoiceNo"] or "") for d in mine])
    if not ids and len(to) > 1:
        return {"name": name, "matched": 0, "reason": "lebih dari satu draf dengan nomor invoice ini: pilih barisnya lalu lampirkan"}
    for pid in to:
        pg.add_file(user, pid, "OTHER", data, name, "INVOICE", None)
    return {"name": name, "matched": len(to), "reason": None if to else "nomor invoice tidak ditemukan di nama file"}


def each(ids: list[str], fn) -> dict:
    """The same step on several requests, each on its own: one refused never stops the others. Pure (tested)."""
    done, failed = [], []
    for i in list(dict.fromkeys(ids))[:MAX_BULK]:
        try:
            done.append(fn(i))
        except Exception as e:  # noqa: BLE001 - told per request
            failed.append({"id": i, "reason": str(getattr(e, "detail", None) or e)[:300]})
    return {"done": done, "failed": failed}


def history(user: dict, data: bytes) -> dict:
    """What was already paid through the Google Form: its invoices are refused here from now on."""
    rows, added = parse(data), 0
    with db.transaction() as conn:
        for r in rows:
            a, i, n = _sig(r["account"], r["invoiceNo"], r["amount"])
            if not a or not i or not n:
                continue
            hit = conn.execute(f"""INSERT INTO {P}.fund_external (account, invoice, amount, label, vendor, created_by) VALUES (%s, %s, %s, %s, %s, %s)
                                   ON CONFLICT (account, invoice, amount) DO NOTHING RETURNING id""",
                               (a, i, n, f"Google Form · {r['vendor']} {r['invoiceNo']}", r["vendor"], user["id"])).fetchone()
            added += 1 if hit else 0
    return {"rows": len(rows), "added": added, "total": int(db.fetchrow(f"SELECT count(*) AS n FROM {P}.fund_external")["n"])}


def template() -> bytes:
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pembayaran Vendor"
    ws.append([label for _, label in COLUMNS])
    for i, c in enumerate(ws[1]):
        c.font = Font(bold=True)
        ws.column_dimensions[c.column_letter].width = 24
        if COLUMNS[i][0] in ("account", "invoiceNo"):  # text: a leading zero stays
            for n in range(2, MAX_ROWS + 2):
                ws.cell(n, i + 1).number_format = "@"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
