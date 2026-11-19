"""Stock List → Transit In / Out (migration 123, 2026-11-19): goods on the way that Cost Control enters by hand; the
monthly Stock List (app/stock_list_month.py) adds them to Items In / Items Out of the branch."""
from datetime import date
from typing import Any, Optional

from app import database as db

T = "integration_portal.cc_stock_transit"


class TransitError(ValueError):
    pass


def _who(user: dict) -> str:
    return user.get("full_name") or user.get("username") or str(user.get("id"))


def _view(r: dict) -> dict:
    return {"id": str(r["id"]), "branch": r["branch_code"], "code": r["item_code"], "name": r.get("item_name") or r["item_code"],
            "unit": r["unit"], "qty": float(r["qty"]), "direction": r["direction"], "date": r["business_date"].isoformat(),
            "note": r["note"], "createdBy": r["created_by"], "createdAt": r["created_at"].isoformat()}


def list_(branches: Optional[list[str]], d_from: date, d_to: date) -> list[dict]:
    rows = db.fetch(
        f"""SELECT t.*, i.name AS item_name FROM {T} t LEFT JOIN kc_master.item i ON i.code = t.item_code
             WHERE t.deleted_at IS NULL AND t.business_date BETWEEN %(a)s AND %(b)s
               AND (%(br)s::text[] IS NULL OR t.branch_code = ANY(%(br)s))
             ORDER BY t.business_date DESC, t.created_at DESC""", {"a": d_from, "b": d_to, "br": branches})
    return [_view(r) for r in rows]


def add(user: dict, body: dict, branches: Optional[list[str]]) -> dict:
    branch = body["branch"].strip()
    if branches is not None and branch not in branches:
        raise TransitError("Cabang di luar akses Anda")
    if body["direction"] not in ("in", "out"):
        raise TransitError("Arah transit harus In atau Out")
    row = db.fetchrow(
        f"""INSERT INTO {T} (branch_code, item_code, unit, qty, direction, business_date, note, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (branch, body["code"].strip(), body["unit"].strip().upper(), body["qty"], body["direction"], body["date"],
         (body.get("note") or "").strip()[:500] or None, _who(user)))
    _changed()
    return _view(row)


def remove(user: dict, transit_id: str, branches: Optional[list[str]]) -> Optional[dict]:
    row = db.fetchrow(
        f"""UPDATE {T} SET deleted_at = now(), deleted_by = %(by)s
             WHERE id::text = %(id)s AND deleted_at IS NULL AND (%(br)s::text[] IS NULL OR branch_code = ANY(%(br)s)) RETURNING *""",
        {"by": _who(user), "id": transit_id, "br": branches})
    if row:
        _changed()
    return _view(row) if row else None


def _changed() -> None:
    from app import running_stock
    running_stock.clear()


def totals(windows: dict[str, tuple[date, date]]) -> dict[str, dict[str, dict[str, Any]]]:
    """{branch: {item code: {"in": [(qty, unit)], "out": [(qty, unit)]}}} of the transit rows inside each branch's window."""
    if not windows:
        return {}
    rows = db.fetch(f"""SELECT branch_code, item_code, unit, qty, direction, business_date FROM {T}
                         WHERE deleted_at IS NULL AND branch_code = ANY(%s) AND business_date BETWEEN %s AND %s""",
                    (list(windows), min(a for a, _ in windows.values()), max(b for _, b in windows.values())))
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        a, b = windows[r["branch_code"]]
        if a <= r["business_date"] <= b:
            item = out.setdefault(r["branch_code"], {}).setdefault(r["item_code"], {"in": [], "out": []})
            item[r["direction"]].append((float(r["qty"]), str(r["unit"] or "").upper()))
    return out
