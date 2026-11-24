"""Fund Request → the lists management sets (migration 124, 2026-11-20): Keperluan Dana (PURPOSE, with its flags and
its own approval flow), Jenis Transaksi (TRANSACTION), Project (PROJECT), Jenis Rekening (ACCOUNT); and the plafon per
department for the purposes marked deptPlafon. An option in use is never removed, only made inactive."""
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError

P = "integration_portal"
LISTS = {"PURPOSE": "Keperluan Dana", "TRANSACTION": "Jenis Transaksi", "PROJECT": "Project", "ACCOUNT": "Jenis Rekening"}
PURPOSE_TYPES = ("ADVANCE", "SETTLEMENT")


class OptionError(CheckInError):
    pass


def _settings(list_: str, raw: Optional[dict]) -> dict:
    if list_ != "PURPOSE":
        return {}
    raw = raw or {}
    kind = raw.get("type")
    return {"receipt": bool(raw.get("receipt")), "outlet": bool(raw.get("outlet")), "deptPlafon": bool(raw.get("deptPlafon")),
            "type": kind if kind in PURPOSE_TYPES else None}


def _view(r: dict) -> dict:
    return {"id": r["id"], "list": r["list"], "name": r["name"], "isActive": r["is_active"], "sort": r["sort"],
            "settings": r["settings"] or {}}


def all_options(active_only: bool = False) -> dict[str, list[dict]]:
    rows = db.fetch(f"SELECT * FROM {P}.fund_option {'WHERE is_active' if active_only else ''} ORDER BY list, sort, lower(name)")
    out: dict[str, list[dict]] = {k: [] for k in LISTS}
    for r in rows:
        out.setdefault(r["list"], []).append(_view(r))
    return out


def get(option_id: Optional[int]) -> Optional[dict]:
    if not option_id:
        return None
    r = db.fetchrow(f"SELECT * FROM {P}.fund_option WHERE id = %s", (option_id,))
    return _view(r) if r else None


def _who(user: dict) -> str:
    return user.get("full_name") or user["username"]


def _name(v: Any) -> str:
    name = " ".join(str(v or "").split())[:120]
    if not name:
        raise OptionError("Nama tidak boleh kosong")
    return name


def create(user: dict, list_: str, name: Any, settings: Optional[dict] = None) -> dict:
    if list_ not in LISTS:
        raise OptionError("Daftar tidak dikenal")
    name = _name(name)
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.fund_option WHERE list = %s AND lower(name) = lower(%s)", (list_, name)):
        raise OptionError(f"{LISTS[list_]} \"{name}\" sudah ada")
    sort = (db.fetchrow(f"SELECT coalesce(max(sort), 0) + 10 AS s FROM {P}.fund_option WHERE list = %s", (list_,)) or {"s": 10})["s"]
    r = db.fetchrow(f"""INSERT INTO {P}.fund_option (list, name, sort, settings, updated_by) VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING *""",
                    (list_, name, sort, json.dumps(_settings(list_, settings)), _who(user)))
    return _view(r)


def update(user: dict, option_id: int, body: dict) -> dict:
    cur = db.fetchrow(f"SELECT * FROM {P}.fund_option WHERE id = %s", (option_id,))
    if cur is None:
        raise LookupError("Pilihan tidak ditemukan")
    name = _name(body["name"]) if "name" in body else cur["name"]
    if name.lower() != cur["name"].lower() and db.fetchrow(
            f"SELECT 1 AS x FROM {P}.fund_option WHERE list = %s AND lower(name) = lower(%s) AND id <> %s", (cur["list"], name, option_id)):
        raise OptionError(f"{LISTS[cur['list']]} \"{name}\" sudah ada")
    settings = _settings(cur["list"], {**(cur["settings"] or {}), **(body.get("settings") or {})}) if "settings" in body else cur["settings"]
    r = db.fetchrow(f"""UPDATE {P}.fund_option SET name = %s, is_active = %s, sort = %s, settings = %s::jsonb, updated_at = now(), updated_by = %s
                        WHERE id = %s RETURNING *""",
                    (name, bool(body.get("isActive", cur["is_active"])), int(body.get("sort", cur["sort"])), json.dumps(settings or {}),
                     _who(user), option_id))
    return _view(r)


def usage(option_id: int) -> int:
    """How many requests and cost lines use an option (2026-11-24): a used one is deactivated, never deleted."""
    r = db.fetchrow(f"""SELECT (SELECT count(*) FROM {P}.pengajuan WHERE %(id)s IN (purpose_id, transaction_type_id, project_id, account_type_id))
                             + (SELECT count(*) FROM {P}.de_cost_item WHERE transaction_id = %(id)s) AS n""", {"id": option_id})
    return int(r["n"] or 0)


def delete(user: dict, option_id: int) -> dict:
    """Deletes an option never used (else it stays, deactivated, so the old requests keep their label)."""
    cur = db.fetchrow(f"SELECT * FROM {P}.fund_option WHERE id = %s", (option_id,))
    if cur is None:
        raise LookupError("Pilihan tidak ditemukan")
    n = usage(option_id)
    if n:
        raise OptionError(f"\"{cur['name']}\" sudah dipakai {n} kali: nonaktifkan saja agar data lama tetap terbaca")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.fund_rule WHERE key = %s", (f"PERSONAL:{option_id}",))
        conn.execute(f"DELETE FROM {P}.approval_step WHERE form = 'pengajuan' AND kind = %s", (f"PERSONAL:{option_id}",))
        conn.execute(f"DELETE FROM {P}.fund_option WHERE id = %s", (option_id,))
    return _view(cur)


def dept_plafons() -> list[dict]:
    return [{"department": r["department"], "amount": float(r["amount"])}
            for r in db.fetch(f"SELECT department, amount FROM {P}.fund_dept_plafon ORDER BY department")]


def dept_plafon(department: Optional[str]) -> Optional[Decimal]:
    if not department:
        return None
    r = db.fetchrow(f"SELECT amount FROM {P}.fund_dept_plafon WHERE lower(department) = lower(%s)", (department,))
    return Decimal(str(r["amount"])) if r else None


def set_dept_plafon(user: dict, department: Any, amount: Any) -> list[dict]:
    dept = _name(department)
    if amount in (None, ""):
        db.fetchrow(f"DELETE FROM {P}.fund_dept_plafon WHERE lower(department) = lower(%s) RETURNING department", (dept,))
        return dept_plafons()
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise OptionError("Plafon harus berupa angka") from None
    if value <= 0:
        raise OptionError("Plafon harus lebih dari 0")
    db.fetchrow(f"""INSERT INTO {P}.fund_dept_plafon (department, amount, updated_by) VALUES (%s, %s, %s)
                    ON CONFLICT (department) DO UPDATE SET amount = EXCLUDED.amount, updated_at = now(), updated_by = EXCLUDED.updated_by
                    RETURNING department""", (dept, value, _who(user)))
    return dept_plafons()


def departments() -> list[str]:
    return [r["d"] for r in db.fetch(f"""SELECT DISTINCT department AS d FROM {P}.user_account
                                          WHERE is_active AND coalesce(department, '') <> '' ORDER BY 1""")]
