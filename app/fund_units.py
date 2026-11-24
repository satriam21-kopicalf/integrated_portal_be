"""The units of a Fund Request and the PT that pays it (migration 148, 2026-11-24, user decision).

    Kopi Calf, Wasgee, Calf On The Road      paid by PT Yuda Prawira Group
    Roastery, Warehouse, Central Kitchen     paid by PT Dayupra Solusi Partner

The unit follows where the request comes from, never who benefits (user decision): a request of a place takes the unit of
its place (kc_master.facility type), a personal or vendor request the unit its requester chooses among theirs. Unit and
payer are written on the request when it is sent, so a later change of the settings never moves an old payment to another
PT. A unit may have its own approval flow: kind "<kind>@<unit>", else the flow of the kind.

user_fund_unit: the units of an account, set in User Accounts. None chosen: requests from the units of its places (COTR
accounts: COTR); who monitors: see monitors(). Chosen: exactly those, for both.
"""
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError
from app.utils import TTLCache

P = "integration_portal"
MAIN, WASGEE, COTR = "CALF", "CALF7", "COTR"
UNIT_OF_TYPE = {"ROASTERY": "ROASTERY", "HUB_WH": "WAREHOUSE", "WAREHOUSE": "WAREHOUSE", "WH_CENTRAL": "WAREHOUSE",
                "HUB_CK": "CK", "CK_ESPRESSO": "CK"}
_cache = TTLCache()


class UnitError(CheckInError):
    pass


def units() -> list[dict]:
    return [{"code": r["code"], "name": r["name"], "payer": r["payer"], "vendor": r["vendor"]}
            for r in db.fetch(f"SELECT code, name, payer, vendor FROM {P}.fund_unit ORDER BY sort, code")]


def get(code: Optional[str]) -> Optional[dict]:
    return next((u for u in units() if u["code"] == code), None)


def _types() -> dict[str, str]:
    hit = _cache.get("types")
    if hit is None:
        hit = {r["code"]: r["facility_type"] for r in db.fetch("SELECT code, facility_type FROM kc_master.facility")}
        _cache.set("types", hit, 300)
    return hit


def unit_of(branch_code: Optional[str], types: dict[str, str], wasgee: Any) -> str:
    """The unit of a place. Pure (tested)."""
    if branch_code and branch_code in wasgee:
        return WASGEE
    return UNIT_OF_TYPE.get(types.get(branch_code or "", ""), MAIN)


def unit_of_branch(branch_code: Optional[str]) -> str:
    from app.scope import brand_branches
    return unit_of(branch_code, _types(), brand_branches(WASGEE))


def chosen(user_id: str) -> list[str]:
    return [r["unit"] for r in db.fetch(f"SELECT unit FROM {P}.user_fund_unit WHERE user_id::text = %s", (str(user_id),))]


def set_chosen(user_id: str, codes: list[str]) -> list[str]:
    known = {u["code"] for u in units()}
    if set(codes) - known:
        raise UnitError("Unit tidak dikenal")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.user_fund_unit WHERE user_id::text = %s", (str(user_id),))
        for c in dict.fromkeys(codes):
            conn.execute(f"INSERT INTO {P}.user_fund_unit (user_id, unit) VALUES (%s, %s)", (str(user_id), c))
    return chosen(user_id)


def monitors(user: dict) -> Optional[set[str]]:
    """The units an account reads in the monitoring; None = every unit. Chosen in User Accounts, else: superadmin / admin
    every unit; the others Kopi Calf, Wasgee and COTR as before, and the units whose flow they are a step of (a step of the
    vendor payment of every unit: the vendor units) — the payables of the other PT are not shown to every monitor."""
    mine = chosen(user["id"])
    if mine:
        return set(mine)
    if user.get("role") in ("superadmin", "admin"):
        return None
    kinds = [r["kind"] for r in db.fetch(f"""SELECT DISTINCT s.kind FROM {P}.approval_step s JOIN {P}.approval_step_user su ON su.step_id = s.id
                                             WHERE s.form = 'pengajuan' AND su.user_id::text = %s""", (str(user["id"]),))]
    out = {MAIN, WASGEE, COTR} | {k.split("@", 1)[1] for k in kinds if "@" in k}
    if "VENDOR" in kinds:
        out |= {u["code"] for u in units() if u["vendor"]}
    return out


def requests_for(user: dict, branches: list[dict]) -> list[str]:
    """The units an account may ask for, in order: the chosen ones, else those of its places, else of its line."""
    mine = set(chosen(user["id"]))
    if not mine:
        if user.get("role") in ("superadmin", "admin"):
            mine = {u["code"] for u in units()}
        elif user.get("business_line") == "cotr":
            mine = {COTR}
        else:
            mine = {unit_of_branch(b["code"]) for b in branches} or {WASGEE if user.get("business_line") == "wasgee" else MAIN}
    return [u["code"] for u in units() if u["code"] in mine]


def in_monitor(user: dict, row: dict) -> bool:
    mine = monitors(user)
    return mine is None or (row.get("unit") or MAIN) in mine


def stamp(conn, pengajuan_id: str, unit: str) -> None:
    """Unit and payer written on the request (when it is sent)."""
    hit = conn.execute(f"""UPDATE {P}.pengajuan p SET unit = u.code, payer = u.payer FROM {P}.fund_unit u
                           WHERE p.id::text = %s AND u.code = %s RETURNING p.id""", (pengajuan_id, unit)).fetchone()
    if hit is None:
        raise UnitError("Unit tidak dikenal")


def set_payer(code: str, payer: Any) -> list[dict]:
    value = " ".join(str(payer or "").split())[:80]
    if not value:
        raise UnitError("Isi nama PT pembayar")
    if db.fetchrow(f"UPDATE {P}.fund_unit SET payer = %s WHERE code = %s RETURNING code", (value, code)) is None:
        raise UnitError("Unit tidak dikenal")
    return units()


def open_counts() -> dict[str, int]:
    return {r["u"]: int(r["n"]) for r in db.fetch(f"""SELECT coalesce(unit, 'CALF') AS u, count(*) AS n FROM {P}.pengajuan
                                                      WHERE status IN ('SUBMITTED', 'REVISION', 'APPROVED', 'PAID', 'VERIFYING') GROUP BY 1""")}


def unit_flows() -> list[str]:
    return [r["kind"] for r in db.fetch(f"SELECT DISTINCT kind FROM {P}.approval_step WHERE form = 'pengajuan' AND position('@' IN kind) > 0")]
