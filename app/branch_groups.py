"""Branch groups of the monitoring (migration 025, 2026-10-21): classes of outlets (CCI, CTG, CTGP …) the PIC Check In,
Monitoring Product and Monitoring Outlet are judged by. One group per branch; kept in Monitoring → Branch groups."""
import re
from typing import Optional

from app import database as db
from app.database import SCHEMA

TG = "integration_portal.branch_group"
TM = "integration_portal.branch_group_member"
CODE_RE = re.compile(r"^[A-Z0-9_-]{1,20}$")


class GroupError(ValueError):
    """A change refused with a message for the user (HTTP 422)."""


def outlets() -> list[dict]:
    """Every active Kopi Calf outlet (ESB branch) with its group."""
    return db.fetch(
        f"""SELECT DISTINCT ON (b.branch_code) b.branch_code AS code, b.branch_name AS name, g.id::text AS group_id, g.code AS group_code
            FROM {SCHEMA}.master_branches b
            LEFT JOIN {TM} m ON m.branch_code = b.branch_code
            LEFT JOIN {TG} g ON g.id = m.group_id
            WHERE COALESCE(b.branch_code, '') <> '' AND COALESCE(b.is_deleted, false) = false AND COALESCE(b.is_active, true)
              AND b.branch_name ILIKE 'Kopi Calf%%'
            ORDER BY b.branch_code, b.branch_name""")


def list_groups() -> dict:
    groups = db.fetch(f"SELECT id::text, code, name, description, sort, updated_at FROM {TG} ORDER BY sort, code")
    branches = outlets()
    by_group: dict[str, list[dict]] = {}
    for b in branches:
        if b["group_id"]:
            by_group.setdefault(b["group_id"], []).append({"code": b["code"], "name": b["name"]})
    out = [{**g, "updated_at": g["updated_at"].isoformat(), "branches": sorted(by_group.get(g["id"], []), key=lambda x: x["name"])} for g in groups]
    return {"groups": out, "unassigned": sorted(({"code": b["code"], "name": b["name"]} for b in branches if not b["group_id"]), key=lambda x: x["name"])}


def _clean(code: str, name: str) -> tuple[str, str]:
    code = (code or "").strip().upper()
    name = " ".join((name or "").split())
    if not CODE_RE.match(code):
        raise GroupError("Kode grup 1-20 karakter: huruf kapital, angka, - atau _")
    if not name:
        raise GroupError("Nama grup wajib diisi")
    return code, name


def save_group(group_id: Optional[str], code: str, name: str, description: Optional[str], sort: int, actor: Optional[str]) -> dict:
    code, name = _clean(code, name)
    clash = db.fetchrow(f"SELECT id::text FROM {TG} WHERE code = %s AND (%s::uuid IS NULL OR id <> %s::uuid)", (code, group_id, group_id))
    if clash:
        raise GroupError(f"Kode {code} sudah dipakai grup lain")
    desc = (description or "").strip()[:300] or None
    if group_id:
        row = db.fetchrow(f"""UPDATE {TG} SET code = %s, name = %s, description = %s, sort = %s, updated_at = now(), updated_by = %s
                              WHERE id::text = %s RETURNING id::text""", (code, name, desc, sort, actor, group_id))
        if row is None:
            raise LookupError("Grup tidak ditemukan")
    else:
        row = db.fetchrow(f"""INSERT INTO {TG} (code, name, description, sort, updated_by) VALUES (%s, %s, %s, %s, %s)
                              RETURNING id::text""", (code, name, desc, sort, actor))
    return {"id": row["id"]}


def delete_group(group_id: str) -> None:
    if db.fetchrow(f"DELETE FROM {TG} WHERE id::text = %s RETURNING id", (group_id,)) is None:
        raise LookupError("Grup tidak ditemukan")


def set_members(group_id: str, codes: list[str]) -> dict:
    """The group's branches become exactly `codes` (a branch moves out of its former group)."""
    if db.fetchrow(f"SELECT 1 AS x FROM {TG} WHERE id::text = %s", (group_id,)) is None:
        raise LookupError("Grup tidak ditemukan")
    known = {b["code"] for b in outlets()}
    unknown = sorted(set(codes) - known)
    if unknown:
        raise GroupError(f"Cabang tidak dikenal: {', '.join(unknown[:5])}")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {TM} WHERE group_id::text = %s AND NOT (branch_code = ANY(%s))", (group_id, codes))
        conn.execute(f"DELETE FROM {TM} WHERE branch_code = ANY(%s) AND group_id::text <> %s", (codes, group_id))
        for c in codes:
            conn.execute(f"INSERT INTO {TM} (branch_code, group_id) VALUES (%s, %s::uuid) ON CONFLICT (branch_code) DO NOTHING", (c, group_id))
    return {"branches": len(codes)}


def codes_of(group: str) -> list[str]:
    """Branch codes of a group (by id or code); "none" = the outlets without a group."""
    if group == "none":
        return [b["code"] for b in outlets() if not b["group_id"]]
    rows = db.fetch(f"""SELECT m.branch_code FROM {TM} m JOIN {TG} g ON g.id = m.group_id
                        WHERE g.id::text = %(g)s OR g.code = %(g)s""", {"g": group})
    return [r["branch_code"] for r in rows]


def narrow(branches: Optional[list[str]], group: Optional[str]) -> Optional[list[str]]:
    """The branches of a monitoring query narrowed to a group (None = every branch)."""
    if not group:
        return branches
    codes = codes_of(group)
    return codes if branches is None else [b for b in branches if b in set(codes)]
