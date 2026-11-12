"""The pool workbooks of COTR ("Cipete - November.xlsx" …) read into the app (migration 089).

Sheet "Product": per day five columns (one per menu) with the rows Product Awal, Product Transfer, Product Datang, Product
Terjual (Armada), Wasting, Cupping, Sisa Product (Hasil Hitungan), Sisa Product (Aktual Fisik), Balanced; under it a block
per day "Tanggal <day>" with the leftover counted per production / batch code (Kode, Qty) of every menu.
Sheet "Report Armada …": per armada a block of 13 rows (the menus, TOTAL PRODUK, TOTAL PENDAPATAN, PENDAPATAN QRIS,
PENDAPATAN CASH, ACTUAL CASH, OPERASIONAL, BALANCE); per day six columns B, S, C, W, T, TOTAL.
"""
import re
from datetime import date, datetime
from io import BytesIO
from typing import Any, Optional

LABELS = {"product awal": "opening", "product transfer": "transfer", "product datang": "arrived", "product terjual": "sold",
          "wasting": "waste", "waste": "waste", "cupping": "cupping", "sisa product (hasil": "calculated", "sisa product (aktual": "actual"}


def menu_of(name: Any, menus: dict[str, dict]) -> Optional[str]:
    """A product name of the sheet to a menu of the master ("Premium" → Calf Premium Cup, "Smooth Vanilla" → Smooth …)."""
    n = " ".join(str(name or "").lower().split())
    if not n:
        return None
    # migration 095: a name mapped by hand in Master Menu wins
    for c, m in menus.items():
        if n in (m.get("esb_aliases") or []):
            return c
    if "banana" in n and not any("banana" in m["name"].lower() for m in menus.values()):
        return None  # a flavour the master does not sell is not another menu

    def has(*words: str) -> list[str]:
        return [c for c, m in menus.items() if all(w in m["name"].lower() for w in words)]
    for words in ((("premium", "can"),) if "premium" in n and "can" in n else (("premium", "cup"), ("premium",)) if "premium" in n else
                  (("smooth",),) if "smooth" in n else (("choco",),) if "choco" in n else (("matcha",),) if "matcha" in n else ()):
        hit = [c for c in has(*words) if not ("premium" in n and "can" not in n and "can" in menus[c]["name"].lower())]
        if hit:
            return hit[0]
    return None


def _int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(round(float(str(v).replace(",", ".").strip())))
    except ValueError:
        return 0


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _code(v: Any) -> Optional[str]:
    """A batch code as typed ('0608, 1008.0, "'0207"): four digits."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return f"{int(v):04d}"
    s = re.sub(r"[^0-9]", "", str(v))
    return s.zfill(4) if s else None


def _month(rows: list[tuple]) -> Optional[date]:
    for r in rows[:5]:
        for c in r:
            if isinstance(c, datetime):
                return c.date().replace(day=1)
    return None


def parse_product(rows: list[tuple], menus: dict[str, dict]) -> dict[date, dict]:
    """{day: {"figures": {menu: {kind: cups}}, "lots": {menu: [(batch, qty)]}}} of the sheet Product."""
    first = _month(rows)
    if not first:
        return {}
    head = next((i for i, r in enumerate(rows) if r and isinstance(r[0], str) and r[0].lower().startswith("product awal")), None)
    if head is None:
        return {}
    names = rows[head - 1]
    out: dict[date, dict] = {}
    for i in range(head, min(head + 9, len(rows))):
        label = str(rows[i][0] or "").lower()
        kind = next((v for k, v in LABELS.items() if label.startswith(k)), None)
        if not kind:
            continue
        for day in range(1, 32):
            col = 1 + 5 * (day - 1)
            try:
                d = first.replace(day=day)
            except ValueError:
                break
            for k in range(5):
                if col + k >= len(rows[i]):
                    continue
                v = rows[i][col + k]
                menu = menu_of(names[col + k] if col + k < len(names) else None, menus)
                if menu and v not in (None, ""):
                    out.setdefault(d, {"figures": {}, "lots": {}})["figures"].setdefault(menu, {})[kind] = _int(v)
    for i, r in enumerate(rows):
        if len(r) > 2 and r[1] == "Tanggal" and isinstance(r[2], datetime):
            d, heads = r[2].date(), rows[i + 1]
            j = i + 3
            while j < len(rows) and rows[j][1] != "Tanggal" and any(c not in (None, "") for c in rows[j][1:11]):
                for k in range(1, 11, 2):
                    menu = menu_of(heads[k], menus)
                    code, qty = _code(rows[j][k]), _int(rows[j][k + 1])
                    if menu and qty:
                        out.setdefault(d, {"figures": {}, "lots": {}})["lots"].setdefault(menu, []).append((code, qty))
                j += 1
    return out


def parse_report(rows: list[tuple], menus: dict[str, dict]) -> list[dict]:
    """The armada of the sheet Report Armada: [{"label", "day", "lines": {menu: {b, s, c, w, t}}, "qris", "cashActual",
    "operational", "operationalNote"}] for the days with something."""
    out = []
    i = 0
    while i < len(rows):
        r = rows[i]
        label = r[1] if len(r) > 2 else None
        if isinstance(label, str) and label.strip().upper().startswith("TOTAL"):  # the totals of the sheet, not an armada
            i += 13
            continue
        if isinstance(label, str) and label.strip() and isinstance(r[2], str) and menu_of(r[2], menus):
            block = rows[i:i + 13]
            by = {str(x[2] or "").strip().upper(): x for x in block}
            for day in range(1, 32):
                col = 4 + 6 * (day - 1)
                lines = {}
                for x in block:
                    menu = menu_of(x[2], menus) if isinstance(x[2], str) and not str(x[2]).upper().startswith(("TOTAL", "PENDAPATAN", "ACTUAL", "OPERASIONAL", "BALANCE")) else None
                    if not menu or col + 4 >= len(x):
                        continue
                    b, s, c, w = (_int(x[col + k]) for k in range(4))
                    t = _int(x[col + 4])
                    if b or s or c or w or t:
                        lines[menu] = {"b": b, "s": s, "c": c, "w": w, "t": t}
                qris = _num(by.get("PENDAPATAN QRIS", ())[col + 5]) if col + 5 < len(by.get("PENDAPATAN QRIS", ())) else None
                cash = _num(by.get("ACTUAL CASH", ())[col + 5]) if col + 5 < len(by.get("ACTUAL CASH", ())) else None
                op_row = by.get("OPERASIONAL", ())
                op = _num(op_row[col + 5]) if col + 5 < len(op_row) else None
                note = op_row[col] if col < len(op_row) and isinstance(op_row[col], str) else None
                if lines or op or cash:
                    out.append({"label": " ".join(label.split()), "day": day, "lines": lines, "qris": qris, "cashActual": cash,
                                "operational": op, "operationalNote": note})
            i += 13
            continue
        i += 1
    return out


def read_workbook(data: bytes, menus: dict[str, dict]) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    product, report, month = None, None, None
    minus: dict[str, dict] = {}
    for name in wb.sheetnames:
        if name.strip().lower() == "product":
            rows = list(wb[name].iter_rows(values_only=True))
            month = _month(rows)
            product = parse_product(rows, menus)
        elif name.lower().startswith("report armada"):
            report = parse_report(list(wb[name].iter_rows(values_only=True)), menus)
        elif name.lower().startswith("rekap minus"):
            for m in parse_minus(list(wb[name].iter_rows(values_only=True))):
                key = (m["armada"] or m["name"]).upper()
                cur = minus.setdefault(key, {**m, "short": 0.0, "over": 0.0, "paid": 0.0})
                for k in ("short", "over", "paid"):
                    cur[k] += m[k]
    return {"month": month, "product": product or {}, "report": report or [], "minus": list(minus.values())}


def parse_minus(rows: list[tuple]) -> list[dict]:
    """A sheet "Rekap Minus …": per employee (name, armada) and day the columns Kekurangan (short), Lebihan (over) and
    Pembayaran (paid back); summed over the days of the sheet."""
    head = next((i for i, r in enumerate(rows[:6]) if any(str(c or "").strip().lower() == "kekurangan" for c in r)), None)
    if head is None:
        return []
    kinds = {j: str(c).strip().lower() for j, c in enumerate(rows[head]) if str(c or "").strip().lower() in ("kekurangan", "lebihan", "pembayaran")}
    out = []
    for r in rows[head + 1:]:
        name = r[1] if len(r) > 1 else None
        if not isinstance(name, str) or not name.strip() or name.strip().upper().startswith("TOTAL"):
            continue
        sums = {"kekurangan": 0.0, "lebihan": 0.0, "pembayaran": 0.0}
        for j, k in kinds.items():
            v = _num(r[j]) if j < len(r) else None
            if v:
                sums[k] += v
        out.append({"name": " ".join(name.split()), "armada": " ".join(str(r[2] or "").split()) if len(r) > 2 else "",
                    "short": sums["kekurangan"], "over": sums["lebihan"], "paid": sums["pembayaran"]})
    return out
