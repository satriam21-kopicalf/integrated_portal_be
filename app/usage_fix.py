"""Usage Management, the side of Cost Control (2026-11-25, migration 152): the lines that look wrong and their correction.

Unusual   a quantity of 10 times or more the usual (median) quantity of the item over the last 60 days, when the item has
          enough lines to tell. Most are a quantity entered in pieces or grams while the unit is a pack: `packSize` is the
          content the unit names (PACK@100PCS = 100, PACK@1KG = 1000 g) and `suggested` the quantity in packs, given only
          when that quantity is usual.
Correct   quantity (0 allowed), item, category, note of a line, one or many at once with one reason. Every changed field
          is logged; a corrected line is not changed again by the outlet (app/waste.py save_usage)."""
import re
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from app import database as db
from app import waste

T = waste.T_USAGE
T_FIX = "integration_portal.usage_correction"
DAYS, TIMES, MIN_LINES, LIMIT = 60, 10, 20, 20_000
_PACK = re.compile(r"@\s*(\d+(?:[.,]\d+)?)\s*([A-Z]+)", re.I)


def pack_size(unit: Optional[str]) -> Optional[float]:
    """The content of a pack by its unit: PACK@100PCS -> 100, PACK@1KG -> 1000 (grams), PCS -> None."""
    m = _PACK.search(unit or "")
    if not m:
        return None
    size = float(m.group(1).replace(",", ".")) * (1000 if m.group(2).upper() in ("KG", "L", "LTR") else 1)
    return size if size > 1 else None


def judge(qty: float, unit: Optional[str], median: Optional[float], lines: int) -> dict:
    """{unusual, packSize, suggested} of a line. Pure (tested)."""
    size = pack_size(unit)
    unusual = bool(median) and lines >= MIN_LINES and qty >= TIMES * median
    packs = round(qty / size, 3) if unusual and size else None
    return {"unusual": unusual, "packSize": size, "suggested": packs if packs and packs < TIMES * median else None}


def usual() -> dict[str, tuple[float, int]]:
    """{item code: (median quantity, lines)} of the last days, every outlet."""
    return {r["item_code"]: (float(r["med"]), r["n"]) for r in db.fetch(
        f"""SELECT item_code, percentile_cont(0.5) WITHIN GROUP (ORDER BY qty)::float AS med, count(*)::int AS n
              FROM {T} WHERE qty > 0 AND business_date >= current_date - %s::int GROUP BY 1""", (DAYS,))}


def report(date_from: date, date_to: date, branches: Optional[list[str]]) -> list[dict]:
    """The Usage Management lines of the period (latest first), each judged."""
    where = "u.business_date BETWEEN %(a)s AND %(b)s" + ("" if branches is None else " AND u.branch_code = ANY(%(br)s)")
    rows = db.fetch(f"""SELECT u.branch_code, u.branch_name, u.business_date, u.item_code, u.item_name, u.unit, u.qty::float AS qty,
                               u.category, u.note, u.photo IS NOT NULL AS has_photo, coalesce(u.full_name, u.username) AS by, u.updated_at,
                               (SELECT count(*) FROM {T_FIX} c WHERE c.branch_code = u.branch_code AND c.business_date = u.business_date
                                   AND c.item_code = u.item_code)::int AS fixes
                          FROM {T} u WHERE {where} ORDER BY u.business_date DESC, u.branch_name, u.item_name LIMIT {LIMIT}""",
                    {"a": date_from, "b": date_to, "br": branches or []})
    med = usual() if rows else {}
    out = []
    for r in rows:
        m, n = med.get(r["item_code"], (None, 0))
        j = judge(r["qty"], r["unit"], m, n)
        if r["fixes"]:  # checked by Cost Control
            j = {**j, "unusual": False, "suggested": None}
        out.append({"branchCode": r["branch_code"], "branchName": r["branch_name"], "businessDate": r["business_date"].isoformat(),
                    "itemCode": r["item_code"], "itemName": r["item_name"], "unit": r["unit"], "qty": r["qty"], "by": r["by"],
                    "category": r["category"], "note": r["note"], "hasPhoto": bool(r["has_photo"]),
                    "updatedAt": r["updated_at"].isoformat(), "corrections": r["fixes"], "usual": m, **j})
    return out


def _num(q: Any) -> str:
    return f"{Decimal(str(q)).normalize():f}"


def correct(lines: list[dict], reason: str, user: dict, branches: Optional[tuple]) -> dict:
    """Corrects the lines {branchCode, businessDate, itemCode, qty?, newItemCode?, category?, note?}; `seen` (the quantity
    on the screen) guards against a line the outlet changed meanwhile. Each line for itself: {updated, skipped}."""
    reason = (reason or "").strip()[:500]
    if len(reason) < 3:
        raise waste.WasteError("Tulis alasan koreksi")
    done, skipped = [], []
    for ln in lines:
        try:
            done.append(_one(ln, reason, user, branches))
        except waste.WasteError as exc:
            skipped.append({"branchCode": ln["branchCode"], "businessDate": ln["businessDate"].isoformat(), "itemCode": ln["itemCode"], "reason": str(exc)})
    return {"updated": done, "skipped": skipped}


def _one(ln: dict, reason: str, user: dict, branches: Optional[tuple]) -> dict:
    branch, day, code = ln["branchCode"], ln["businessDate"], ln["itemCode"]
    if branches is not None and branch not in branches:
        raise waste.WasteError("Cabang di luar akses Anda")
    key = (branch, day, code)
    at = "branch_code = %s AND business_date = %s AND item_code = %s"
    target = ln.get("newItemCode") or code
    cat = waste.catalog(branch=branch) if target != code or ln.get("category") is not None else None
    with db.transaction() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT branch_name, item_name, unit, qty, category, note FROM {T} WHERE {at} FOR UPDATE", key)
        row = cur.fetchone()
        if row is None:
            raise waste.WasteError("Baris Usage Management tidak ditemukan")
        if ln.get("seen") is not None and abs(float(row["qty"]) - float(ln["seen"])) > 0.0005:
            raise waste.WasteError("Jumlah sudah berubah, muat ulang halaman")
        new: dict[str, Any] = {}
        log: list[tuple[str, Optional[str], Optional[str]]] = []
        unit = row["unit"]
        if target != code:
            item = next((i for i in cat["items"]["usage"] if i["code"] == target), None)
            if item is None:
                raise waste.WasteError("Item tidak ada di daftar Usage Management")
            cur.execute(f"SELECT 1 AS x FROM {T} WHERE {at}", (branch, day, target))
            if cur.fetchone():
                raise waste.WasteError(f"{item['name']} sudah ada di cabang dan hari itu: koreksi jumlahnya di baris itu")
            log.append(("item", f"{code} {row['item_name']}", f"{item['code']} {item['name']}"))
            new.update(item_code=item["code"], item_name=item["name"], unit=item["unit"])
            unit = item["unit"]
        if ln.get("qty") is not None:
            q = waste._qty(ln["qty"], allow_zero=True)
            if q != row["qty"] or unit != row["unit"]:
                log.append(("qty", f"{_num(row['qty'])} {row['unit']}", f"{_num(q)} {unit}"))
                new["qty"] = q
        if ln.get("category") is not None and ln["category"] != row["category"]:
            if ln["category"] not in cat["usageCategories"]:
                raise waste.WasteError("Pilih kategori Usage Management")
            log.append(("category", row["category"], ln["category"]))
            new["category"] = ln["category"]
        if "note" in ln and ((ln["note"] or "").strip()[:500] or None) != row["note"]:
            note = (ln["note"] or "").strip()[:500] or None
            log.append(("note", row["note"], note))
            new["note"] = note
        if not log:
            raise waste.WasteError("Tidak ada perubahan")
        sets = ", ".join(f"{k} = %s" for k in new)
        cur.execute(f"UPDATE {T} SET {sets} WHERE {at}", (*new.values(), *key))  # earlier corrections follow (ON UPDATE CASCADE)
        by = user.get("full_name") or user["username"]
        cur.executemany(f"INSERT INTO {T_FIX} (branch_code, business_date, item_code, field, old_value, new_value, reason, by_name, by_user) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [(branch, day, target, f, o, n, reason, by, user.get("id")) for f, o, n in log])
    return {"branchCode": branch, "branchName": row["branch_name"], "businessDate": day.isoformat(), "itemCode": target,
            "changed": [f for f, _, _ in log]}


def history(branch: str, day: date, item: str) -> list[dict]:
    rows = db.fetch(f"""SELECT field, old_value, new_value, reason, by_name, created_at FROM {T_FIX}
                        WHERE branch_code = %s AND business_date = %s AND item_code = %s ORDER BY created_at, id""", (branch, day, item))
    return [{"field": r["field"], "old": r["old_value"], "new": r["new_value"], "reason": r["reason"], "by": r["by_name"],
             "at": r["created_at"].isoformat()} for r in rows]


def locked(branch: str, day: date) -> set[str]:
    """The items of a branch and day Cost Control corrected: the outlet does not change them again."""
    return {r["item_code"] for r in db.fetch(f"SELECT DISTINCT item_code FROM {T_FIX} WHERE branch_code = %s AND business_date = %s", (branch, day))}
