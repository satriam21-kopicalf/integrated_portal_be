"""PIC Check In (Form → Visit) and its monitoring (integration_portal.pic_checkin, migration 019).

A PIC checks in at the outlet he visits:
  * who: the signed-in account (name and username, not typed);
  * where: one of the user's branches (role user: its assigned branches; super admin / Cost Control: every branch);
  * when: the server time of the check-in (the phone's clock is kept beside it);
  * evidence: a camera selfie stamped in the browser with the time and the branch; the server counts the faces
    (app/face.py) and refuses a photo without one;
  * position: GPS with its accuracy, compared with the outlet's coordinates set in Cost Control → Lokasi Outlet
    (kopicalf kc_master.facility, linked to the ESB branch through facility_esb_map). Same rule as the stock opname
    (kopicalf 06.9 §12.15): outside the radius when distance − min(accuracy, 100 m) > radius; an outlet without
    coordinates cannot be checked (no distance, not outside); accuracy worse than 100 m = low accuracy.
"""
import base64
import binascii
import math
import re
from datetime import date, datetime
from typing import Any, Optional

from app import database as db
from app.avatars import _signature_ok
from app.database import SCHEMA
from app.face import count_faces

T = "integration_portal.pic_checkin"
MAX_PHOTO = 3 * 1024 * 1024
LOW_ACCURACY_M = 100
ACCURACY_CREDIT_M = 100
GLOBAL_ROLES = ("superadmin", "cost_control")
_DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\s]+)$")


class CheckInError(ValueError):
    """A check-in refused with a message for the PIC (HTTP 422)."""


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)))


def judge(lat: float, lng: float, accuracy: Optional[float], outlet: Optional[dict]) -> dict:
    """distance_m, outside_radius, low_accuracy for a position against an outlet ({latitude, longitude, radius_m})."""
    low = accuracy is not None and accuracy > LOW_ACCURACY_M
    if not outlet or outlet.get("latitude") is None or outlet.get("longitude") is None:
        return {"distance_m": None, "outside_radius": False, "low_accuracy": low}
    d = distance_m(lat, lng, float(outlet["latitude"]), float(outlet["longitude"]))
    credit = min(float(accuracy or 0), ACCURACY_CREDIT_M)
    return {"distance_m": d, "outside_radius": d - credit > int(outlet.get("radius_m") or 100), "low_accuracy": low}


# ---------------------------------------------------------------- branches of the form

def _names(codes: list[str]) -> dict[str, str]:
    rows = db.fetch(
        f"""SELECT branch_code AS code, branch_name AS name FROM {SCHEMA}.master_branches
            WHERE branch_code = ANY(%(c)s) AND COALESCE(is_deleted, false) = false
            UNION ALL SELECT code, name FROM kc_master.facility WHERE code = ANY(%(c)s)""", {"c": codes})
    return {r["code"]: r["name"] for r in rows}


def locations(codes: list[str]) -> dict[str, dict]:
    """Outlet coordinates and check-in radius per branch code (ESB branch codes and kopicalf facility codes)."""
    if not codes:
        return {}
    rows = db.fetch(
        f"""SELECT b.branch_code AS code, f.latitude, f.longitude, f.checkin_radius_m AS radius_m
            FROM {SCHEMA}.master_branches b
            JOIN kc_master.facility_esb_map m ON m.company_code = b.company_code AND m.esb_id = b.branch_id::text
            JOIN kc_master.facility f ON f.id = m.facility_id
            WHERE b.branch_code = ANY(%(c)s)
            UNION ALL
            SELECT f.code, f.latitude, f.longitude, f.checkin_radius_m FROM kc_master.facility f WHERE f.code = ANY(%(c)s)""",
        {"c": codes})
    out: dict[str, dict] = {}
    for r in rows:
        if r["code"] not in out or (out[r["code"]]["latitude"] is None and r["latitude"] is not None):
            out[r["code"]] = {"latitude": float(r["latitude"]) if r["latitude"] is not None else None,
                              "longitude": float(r["longitude"]) if r["longitude"] is not None else None,
                              "radius_m": r["radius_m"]}
    return out


def branch_options(user: dict) -> list[dict]:
    """Branches the user may check in at, with the outlet's location."""
    if user.get("role") in GLOBAL_ROLES:
        rows = db.fetch(
            f"""SELECT branch_code AS code, branch_name AS name FROM {SCHEMA}.master_branches
                WHERE COALESCE(branch_code, '') <> '' AND COALESCE(is_deleted, false) = false AND COALESCE(is_active, true)
                  AND branch_name ILIKE 'Kopi Calf%%' ORDER BY branch_name""")
        names = {r["code"]: r["name"] for r in rows}
    else:
        codes = list(user.get("branch_codes") or [])
        names = _names(codes)
        names = {c: names.get(c, c) for c in codes}
    locs = locations(list(names))
    return sorted(({"code": c, "name": n, **locs.get(c, {"latitude": None, "longitude": None, "radius_m": None})}
                   for c, n in names.items()), key=lambda b: b["name"])


# ---------------------------------------------------------------- photo

def parse_photo(value: str) -> tuple[str, bytes]:
    match = _DATA_URL.match((value or "").strip())
    if not match:
        raise CheckInError("Foto harus dari kamera (JPG, PNG atau WebP)")
    try:
        data = base64.b64decode(re.sub(r"\s", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise CheckInError("Data foto rusak") from None
    if len(data) > MAX_PHOTO:
        raise CheckInError("Ukuran foto maksimal 3 MB")
    if not _signature_ok(match.group(1), data):
        raise CheckInError("File bukan gambar yang valid")
    return match.group(1), data


def face_check(value: str) -> int:
    """Faces in a photo (raises CheckInError when the photo cannot be read)."""
    _, data = parse_photo(value)
    faces = count_faces(data)
    if faces is None:
        raise CheckInError("Foto tidak dapat dibaca, ambil ulang")
    return faces


# ---------------------------------------------------------------- check in

def create(user: dict, branch_code: str, photo: str, lat: float, lng: float, accuracy: Optional[float],
           located_at: Optional[datetime], device_time: Optional[datetime], note: Optional[str],
           device: Optional[str], ip: Optional[str]) -> dict:
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise CheckInError("Cabang tidak tersedia untuk akun Anda")
    photo_type, data = parse_photo(photo)
    faces = count_faces(data)
    if faces is None:
        raise CheckInError("Foto tidak dapat dibaca, ambil ulang")
    if faces == 0:
        raise CheckInError("Wajah tidak terdeteksi. Ambil ulang foto dengan wajah Anda terlihat jelas")
    j = judge(lat, lng, accuracy, branch)
    row = db.fetchrow(
        f"""INSERT INTO {T} (user_id, username, full_name, role, branch_code, branch_name, device_time, latitude, longitude,
                accuracy_m, located_at, outlet_latitude, outlet_longitude, radius_m, distance_m, outside_radius, low_accuracy,
                face_count, photo, photo_type, note, device, ip)
            VALUES (%(u)s, %(un)s, %(fn)s, %(role)s, %(bc)s, %(bn)s, %(dt)s, %(lat)s, %(lng)s, %(acc)s, %(la)s, %(olat)s, %(olng)s,
                %(r)s, %(d)s, %(out)s, %(low)s, %(faces)s, %(photo)s, %(pt)s, %(note)s, %(dev)s, %(ip)s)
            RETURNING id::text, visited_at""",
        {"u": user["id"], "un": user["username"], "fn": user.get("full_name"), "role": user["role"], "bc": branch["code"],
         "bn": branch["name"], "dt": device_time, "lat": lat, "lng": lng, "acc": accuracy, "la": located_at,
         "olat": branch["latitude"], "olng": branch["longitude"], "r": branch["radius_m"], "d": j["distance_m"],
         "out": j["outside_radius"], "low": j["low_accuracy"], "faces": faces, "photo": data, "pt": photo_type,
         "note": (note or "").strip()[:500] or None, "dev": (device or "")[:300] or None, "ip": ip})
    return {"id": row["id"], "visitedAt": row["visited_at"].isoformat(), "branchCode": branch["code"], "branchName": branch["name"],
            "faces": faces, "outletLocated": branch["latitude"] is not None, "radiusM": branch["radius_m"], **_camel(j)}


def _camel(j: dict) -> dict:
    return {"distanceM": j["distance_m"], "outsideRadius": j["outside_radius"], "lowAccuracy": j["low_accuracy"]}


# ---------------------------------------------------------------- monitoring

COLUMNS = """c.id::text, c.user_id::text, c.username, c.full_name, c.role, c.branch_code, c.branch_name, c.visited_at,
    c.device_time, c.latitude, c.longitude, c.accuracy_m, c.located_at, c.outlet_latitude, c.outlet_longitude, c.radius_m,
    c.distance_m, c.outside_radius, c.low_accuracy, c.face_count, c.note"""


def _view(r: dict) -> dict:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "id": r["id"], "userId": r["user_id"], "username": r["username"], "name": r["full_name"] or r["username"],
        "role": r["role"], "branchCode": r["branch_code"], "branchName": r["branch_name"],
        "visitedAt": r["visited_at"].isoformat(), "deviceTime": r["device_time"].isoformat() if r["device_time"] else None,
        "latitude": num(r["latitude"]), "longitude": num(r["longitude"]), "accuracyM": num(r["accuracy_m"]),
        "outletLatitude": num(r["outlet_latitude"]), "outletLongitude": num(r["outlet_longitude"]), "radiusM": r["radius_m"],
        "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"], "lowAccuracy": r["low_accuracy"],
        "outletLocated": r["outlet_latitude"] is not None, "faces": r["face_count"], "note": r["note"],
    }


def recent_of(user_id: str, limit: int = 10) -> list[dict]:
    return [_view(r) for r in db.fetch(
        f"SELECT {COLUMNS} FROM {T} c WHERE c.user_id = %s ORDER BY c.visited_at DESC LIMIT %s", (user_id, limit))]


def monitor(date_from: date, date_to: date, branches: Optional[list[str]], user_id: Optional[str] = None,
            location: str = "", limit: int = 1000) -> dict:
    """Check-ins of a period (WIB days) for the branches (None = all) with the figures of the period."""
    where = ["(c.visited_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "lim": limit}
    if branches is not None:
        where.append("c.branch_code = ANY(%(br)s)")
        params["br"] = branches
    if user_id:
        where.append("c.user_id::text = %(u)s")
        params["u"] = user_id
    if location == "outside":
        where.append("c.outside_radius")
    elif location == "low":
        where.append("c.low_accuracy")
    elif location == "unlocated":
        where.append("c.outlet_latitude IS NULL")
    cond = " AND ".join(where)
    rows = db.fetch(f"SELECT {COLUMNS} FROM {T} c WHERE {cond} ORDER BY c.visited_at DESC LIMIT %(lim)s", params)
    s = db.fetchrow(
        f"""SELECT count(*)::int AS total, count(DISTINCT c.user_id)::int AS pics, count(DISTINCT c.branch_code)::int AS branches,
                   count(*) FILTER (WHERE c.outside_radius)::int AS outside, count(*) FILTER (WHERE c.low_accuracy)::int AS low,
                   count(*) FILTER (WHERE c.outlet_latitude IS NULL)::int AS unlocated,
                   count(*) FILTER (WHERE NOT c.outside_radius AND c.outlet_latitude IS NOT NULL)::int AS inside
            FROM {T} c WHERE {cond}""", params)
    pics = db.fetch(
        f"""SELECT c.user_id::text AS id, COALESCE(max(c.full_name), max(c.username)) AS name, count(*)::int AS n
            FROM {T} c WHERE {cond} GROUP BY c.user_id ORDER BY 3 DESC, 2""", params)
    return {"data": [_view(r) for r in rows], "summary": s, "pics": pics, "truncated": len(rows) >= limit}


def photo_of(checkin_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT user_id::text, branch_code, photo, photo_type FROM {T} WHERE id::text = %s", (checkin_id,))
