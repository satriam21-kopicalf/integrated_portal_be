"""Recipe cards from an Excel file (Sales Calculator by Menu → Recipe Cards, 2026-11-18): Cost Control fills the sheet the
page exports (one row per line of a card) and imports it to make or change many cards at once.

Columns (the header row is found by its titles, Indonesian or English): Kode Kartu, Nama Kartu, Kategori, Aktif,
Catatan, Jenis Baris (Bahan / WIP / Lain / Menu), Kode Baris, Nama Baris, Qty, Satuan, Harga (Lain only). Rows of one card
share its code; the card's own fields are read from its first row. A card whose code exists is updated (its menus stay
as they are), else it is made. `dry` checks everything and saves nothing: unknown item / WIP / card codes, a missing qty,
a bundle that holds itself are reported per row, and a card with any error is never saved."""
from io import BytesIO
from typing import Optional

from openpyxl import load_workbook

from app import database as db
from app import menu_costing as mc

P = mc.P
MAX_ROWS = 20000
HEAD = {
    "card": ("kode kartu", "card code", "kode resep", "recipe code"),
    "name": ("nama kartu", "card name", "nama resep", "recipe name"),
    "category": ("kategori", "category"),
    "active": ("aktif", "active"),
    "note": ("catatan", "note"),
    "type": ("jenis baris", "line type", "jenis", "type"),
    "code": ("kode baris", "line code", "kode item", "item code"),
    "lname": ("nama baris", "line name", "nama item", "item name"),
    "qty": ("qty", "jumlah", "quantity"),
    "unit": ("satuan", "unit"),
    "price": ("harga", "price"),
}
TYPES = {"bahan": "item", "bahan baku": "item", "item": "item", "packaging": "item", "wip": "wip", "premix": "wip",
         "lain": "other", "lainnya": "other", "other": "other", "menu": "menu", "bundling": "menu"}
NO = {"tidak", "no", "n", "false", "0", "nonaktif", "inactive"}


class ImportError_(ValueError):
    """The file itself cannot be read (HTTP 422)."""


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _num(v) -> Optional[float]:
    if v is None or _text(v) == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _text(v).replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def read(data: bytes) -> list[dict]:
    """The rows of the first sheet under the header row, as {field: value, "row": number in the sheet}."""
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # not an xlsx
        raise ImportError_("File bukan Excel (.xlsx) yang bisa dibaca") from exc
    ws = wb.worksheets[0]
    cols: dict[str, int] = {}
    out: list[dict] = []
    for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not cols:
            names = [_text(c).lower() for c in row]
            found = {f: names.index(h) for f, hs in HEAD.items() for h in hs if h in names}
            if "card" in found and "code" in found:
                cols = found
            continue
        if n > MAX_ROWS:
            raise ImportError_(f"Maksimal {MAX_ROWS} baris sekali import")
        rec = {f: (row[i] if i < len(row) else None) for f, i in cols.items()}
        if not any(_text(v) for v in rec.values()):
            continue
        rec["row"] = n
        out.append(rec)
    if not cols:
        raise ImportError_("Kolom 'Kode Kartu' dan 'Kode Baris' tidak ditemukan: pakai file dari tombol Export")
    return out


def plan(rows: list[dict]) -> list[dict]:
    """The cards of the rows, each with its lines, its action (create | update) and its errors."""
    items = {i["code"]: i for i in mc.items(status="all")["data"]}
    wips = mc.wips()
    existing = {r["code"] for r in db.fetch(f"SELECT code FROM {P}.cc_menu_recipe")}
    cards: dict[str, dict] = {}
    for r in rows:
        code = _text(r.get("card")).upper()
        if not code:
            continue
        c = cards.get(code)
        if c is None:
            c = cards[code] = {"code": code, "name": " ".join(_text(r.get("name")).split()), "category": _text(r.get("category")) or None,
                               "isActive": _text(r.get("active")).lower() not in NO, "note": _text(r.get("note")) or None,
                               "lines": [], "errors": [], "action": "update" if code in existing else "create", "rows": 0}
            if not c["name"]:
                c["errors"].append(f"Baris {r['row']}: nama kartu kosong")
        c["rows"] += 1
        lcode = _text(r.get("code"))
        if not lcode:
            continue  # a card row without a line (header only)
        kind = TYPES.get(_text(r.get("type")).lower(), "item" if lcode in items else "wip" if lcode in wips else None)
        if kind is None:
            c["errors"].append(f"Baris {r['row']}: jenis baris '{_text(r.get('type'))}' tidak dikenal (Bahan / WIP / Lain / Menu)")
            continue
        qty = _num(r.get("qty"))
        if not qty or qty <= 0:
            c["errors"].append(f"Baris {r['row']}: qty {lcode} harus lebih dari 0")
            continue
        line = {"type": kind, "code": lcode, "qty": qty, "name": _text(r.get("lname")), "unit": _text(r.get("unit"))}
        if kind == "item":
            it = items.get(lcode)
            if not it:
                c["errors"].append(f"Baris {r['row']}: kode bahan {lcode} tidak ada di master item")
                continue
            line["name"], line["unit"] = it["name"], it["unit"] or line["unit"]
        elif kind == "wip":
            w = wips.get(lcode)
            if not w:
                c["errors"].append(f"Baris {r['row']}: WIP / premix {lcode} tidak ditemukan (resep aktif)")
                continue
            line["name"] = w["name"]
        elif kind == "menu":
            line["code"] = lcode.upper()
            if line["code"] not in existing and line["code"] not in cards:
                c["errors"].append(f"Baris {r['row']}: kartu {line['code']} untuk bundling tidak ditemukan")
                continue
        else:
            line["name"] = line["name"] or lcode
            line["price"] = _num(r.get("price"))
        c["lines"].append(line)
    for c in cards.values():
        if not c["lines"] and not c["errors"]:
            c["errors"].append("Tidak ada baris bahan")
    return list(cards.values())


def run(data: bytes, dry: bool, user: dict) -> dict:
    cards = plan(read(data))
    saved, failed = 0, 0
    if not dry:
        # the cards a bundle points to first, so a bundle of a new card finds it
        order = sorted(cards, key=lambda c: any(ln["type"] == "menu" for ln in c["lines"]))
        for c in order:
            if c["errors"]:
                continue
            body = {k: c[k] for k in ("code", "name", "category", "lines", "note", "isActive")}
            try:
                mc.save_recipe(c["code"] if c["action"] == "update" else None, body, user)
                saved += 1
            except mc.CostingError as exc:
                c["errors"].append(str(exc))
                failed += 1
    return {"dry": dry, "cards": [{k: c[k] for k in ("code", "name", "action", "errors", "rows")} | {"lines": len(c["lines"])} for c in cards],
            "create": sum(c["action"] == "create" and not c["errors"] for c in cards),
            "update": sum(c["action"] == "update" and not c["errors"] for c in cards),
            "invalid": sum(bool(c["errors"]) for c in cards), "saved": saved, "failed": failed}
