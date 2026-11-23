"""Calf On The Road ← HRMS attendance (stage 3, 2026-11-23): the punch-in / out of every COTR employee in the HRMS
attendance app (sk_hrms.attendance_records, face and GPS checked there), read beside the shifts of the driver app.

The COTR employees are the rows of Master Driver (cotr_driver) whose account is linked to an HRMS employee
(user_account.hrms_employee_id, app/hrms_sync.py). For a day and a pool: who punched in, when, late or not, whether the
punch is at their pool (distance to the pool's coordinates, within its radius), the punch out, and whether a shift of
the driver app was started — so the COTR team checks the attendance of the fleet in Monitoring, and the move to the
pool check in (stage 4) can be followed: who punched at the pool but has no shift yet.
"""
from datetime import date
from math import asin, cos, radians, sin, sqrt
from typing import Optional

from app import database as db

P = "integration_portal"
H = "sk_hrms"
DEFAULT_RADIUS = 150  # m, when the pool has no radius of its own


def distance_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in metres."""
    la1, la2 = radians(a_lat), radians(b_lat)
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin(radians(b_lng - a_lng) / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(h))


def where_of(punch: dict, pool: dict) -> tuple[Optional[int], Optional[bool]]:
    """(metres from the pool, at the pool) of a punch; None when either has no coordinates."""
    if punch.get("lat") is None or punch.get("lng") is None or pool.get("latitude") is None or pool.get("longitude") is None:
        return None, None
    d = distance_m(float(punch["lat"]), float(punch["lng"]), float(pool["latitude"]), float(pool["longitude"]))
    return round(d), d <= float(pool.get("radius_m") or DEFAULT_RADIUS)


def day(d: date, pool: Optional[str] = None) -> dict:
    """Every COTR employee of the pool (or of every pool) on day `d`: the HRMS punch and the shift of the driver app."""
    pools = {r["code"]: r for r in db.fetch(f"SELECT code, name, latitude, longitude, radius_m FROM {P}.cotr_pool")}
    people = db.fetch(
        f"""SELECT d.code, d.name, d.nickname, d.pool_code, d.fleet_code, d.position, u.hrms_employee_id AS emp
              FROM {P}.cotr_driver d LEFT JOIN {P}.user_account u ON u.id::text = d.user_id::text
             WHERE coalesce(d.status, 'active') NOT IN ('inactive', 'resigned', 'keluar')
               AND (%(p)s::text IS NULL OR d.pool_code = %(p)s)
             ORDER BY d.pool_code, d.name""", {"p": pool})
    emps = [p["emp"] for p in people if p["emp"]]
    punches = {}
    if emps:
        for r in db.fetch(f"""SELECT employee_id, clock_in, clock_out, late_minutes, latitude AS lat, longitude AS lng,
                                     face_ok, geofence_ok, status
                                FROM {H}.attendance_records WHERE date = %s AND employee_id = ANY(%s)""", (d, emps)):
            punches[r["employee_id"]] = r
    shifts = {r["driver_code"]: r for r in db.fetch(
        f"SELECT driver_code, started_at, ended_at FROM {P}.cotr_shift WHERE day = %s", (d,))}
    rows = []
    for p in people:
        a = punches.get(p["emp"]) if p["emp"] else None
        meters, near = where_of(a, pools.get(p["pool_code"]) or {}) if a else (None, None)
        # at the pool: the HRMS geofence decides (it accepted the punch at a site); the distance to the coordinates of
        # Master Pool is shown beside it — every punch of 2026-11-23 sat ~280 m from them: the pool points need a check
        at_pool = a["geofence_ok"] if a and a["geofence_ok"] is not None else near
        s = shifts.get(p["code"])
        rows.append({
            "code": p["code"], "name": p["name"], "nickname": p["nickname"], "pool": p["pool_code"],
            "poolName": (pools.get(p["pool_code"]) or {}).get("name") or p["pool_code"], "fleet": p["fleet_code"], "position": p["position"],
            "linked": bool(p["emp"]),
            # clock_in / clock_out are WIB without a zone in HRMS: sent as HH:MM, never shifted by the browser
            "punchIn": a["clock_in"].strftime("%H:%M") if a and a["clock_in"] else None,
            "punchOut": a["clock_out"].strftime("%H:%M") if a and a["clock_out"] else None,
            "late": int(a["late_minutes"] or 0) if a else 0, "faceOk": a["face_ok"] if a else None,
            "meters": meters, "atPool": at_pool,
            "shift": None if not s else ("closed" if s["ended_at"] else "open"),
            "shiftAt": s["started_at"].isoformat() if s else None, "nearPool": near,
        })
    n = lambda f: sum(1 for r in rows if f(r))  # noqa: E731
    return {"day": d.isoformat(), "rows": rows, "summary": {
        "people": len(rows), "linked": n(lambda r: r["linked"]), "punched": n(lambda r: r["punchIn"]),
        "atPool": n(lambda r: r["atPool"]), "late": n(lambda r: r["late"] > 0),
        "shiftNoPunch": n(lambda r: r["shift"] and not r["punchIn"]), "punchNoShift": n(lambda r: r["punchIn"] and not r["shift"])}}
