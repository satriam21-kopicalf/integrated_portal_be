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
import json
import math
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
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
WIB_TZ = ZoneInfo("Asia/Jakarta")
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


# ---------------------------------------------------------------- location risk (fake GPS and the like)
#
# A web page cannot read the phone's "mock location" flag (only a native app can), so a fake position is recognised
# by what it leaves behind. Every check-in gets risk signals; one "high" or "medium" signal shows it in Monitoring for a
# manager to review. The PIC is not told which signal fired (that would teach how to get around it).

RISK_LEVELS = ("none", "low", "medium", "high")
MOVE_KMH = 150          # faster than this between two check-ins of one PIC: impossible travel
MOVE_MIN_KM = 5         # ...over at least this distance (GPS noise, neighbouring outlets)
STALE_MIN = 5           # position taken this long before sending
SKEW_MIN = 5            # phone clock this far from the server clock
STATIC_SAMPLES = 5      # at least this many GPS readings over STATIC_SECONDS without any movement or accuracy change
STATIC_SECONDS = 10
_MOBILE_UA = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.I)


def previous_positions(user_id: str, limit: int = 300) -> list[dict]:
    """The PIC's latest check-ins: position and time (for repeated positions and impossible travel)."""
    return db.fetch(f"""SELECT id::text, latitude, longitude, visited_at, branch_name FROM {T}
                        WHERE user_id::text = %s ORDER BY visited_at DESC LIMIT %s""", (user_id, limit))


def _signal(code: str, level: str, text: str) -> dict:
    return {"code": code, "level": level, "text": text}


def assess(user_id: str, lat: float, lng: float, accuracy: Optional[float], outlet: Optional[dict], now: datetime,
           located_at: Optional[datetime], sent_at: Optional[datetime], samples: Optional[list], device: Optional[str],
           touch: Optional[bool], webdriver: Optional[bool], history: Optional[list[dict]] = None) -> dict:
    """Risk signals of a check-in position: {"level": none|low|medium|high, "signals": [{code, level, text}]}."""
    aware = lambda t: t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)  # noqa: E731
    located_at, sent_at = aware(located_at), aware(sent_at)
    out: list[dict] = []
    if webdriver:
        out.append(_signal("webdriver", "high", "Browser dikendalikan otomatis (automation)"))
    if outlet and outlet.get("latitude") is not None:
        if distance_m(lat, lng, float(outlet["latitude"]), float(outlet["longitude"])) <= 1:
            out.append(_signal("exact_outlet", "high", "Koordinat persis sama dengan titik outlet"))
    history = previous_positions(user_id) if history is None else history
    same = next((h for h in history if round(float(h["latitude"]), 6) == round(lat, 6)
                 and round(float(h["longitude"]), 6) == round(lng, 6)), None)
    if same:
        out.append(_signal("repeat_position", "high",
                           f"Koordinat persis sama dengan check in {same['visited_at'].astimezone(WIB_TZ):%d/%m/%Y %H:%M} "
                           f"({same['branch_name']})"))
    last = history[0] if history else None
    if last:
        km = distance_m(lat, lng, float(last["latitude"]), float(last["longitude"])) / 1000
        hours = max((now - last["visited_at"]).total_seconds() / 3600, 1 / 60)
        if km >= MOVE_MIN_KM and km / hours > MOVE_KMH:
            out.append(_signal("impossible_travel", "high",
                               f"Berpindah {km:.0f} km dari check in sebelumnya ({last['branch_name']}) dalam "
                               f"{hours * 60:.0f} menit"))
    if sent_at and located_at and (sent_at - located_at).total_seconds() > STALE_MIN * 60:
        out.append(_signal("stale_location", "medium",
                           f"Lokasi diambil {(sent_at - located_at).total_seconds() / 60:.0f} menit sebelum dikirim"))
    if sent_at and abs((sent_at - now).total_seconds()) > SKEW_MIN * 60:
        out.append(_signal("clock_skew", "medium", f"Jam HP berbeda {abs((sent_at - now).total_seconds()) / 60:.0f} menit "
                                                   "dari jam server"))
    if device and not _MOBILE_UA.search(device) and not touch:
        out.append(_signal("desktop", "medium", "Dikirim dari komputer, bukan HP (lokasi komputer mudah diubah)"))
    readings = {(s.get("t"), s.get("lat"), s.get("lng"), s.get("acc")) for s in (samples or [])
                if isinstance(s, dict) and s.get("t") is not None}
    if len(readings) >= STATIC_SAMPLES:
        times = [r[0] for r in readings]
        span = (max(times) - min(times)) / 1000
        if span >= STATIC_SECONDS and len({(r[1], r[2], r[3]) for r in readings}) == 1:
            out.append(_signal("static_fix", "medium",
                               f"{len(readings)} pembacaan GPS selama {span:.0f} detik tanpa perubahan sedikit pun "
                               "(pola aplikasi lokasi palsu)"))
    if accuracy is not None and accuracy < 2:
        out.append(_signal("perfect_accuracy", "low", f"Akurasi GPS ±{accuracy:g} m, tidak wajar untuk browser"))
    level = max((s["level"] for s in out), key=RISK_LEVELS.index, default="none")
    return {"level": level, "signals": out}


# ---------------------------------------------------------------- check in

def create(user: dict, branch_code: str, photo: str, lat: float, lng: float, accuracy: Optional[float],
           located_at: Optional[datetime], device_time: Optional[datetime], note: Optional[str],
           device: Optional[str], ip: Optional[str], sent_at: Optional[datetime] = None, samples: Optional[list] = None,
           touch: Optional[bool] = None, webdriver: Optional[bool] = None) -> dict:
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
    note = (note or "").strip()[:500] or None
    if j["outside_radius"] and not note:
        raise CheckInError(f"Anda berada {j['distance_m']} m dari outlet, di luar radius {branch['radius_m']} m. "
                           "Tulis alasannya di catatan untuk tetap check in")
    risk = assess(str(user["id"]), lat, lng, accuracy, branch, datetime.now(WIB_TZ), located_at, sent_at, samples, device,
                  touch, webdriver)
    row = db.fetchrow(
        f"""INSERT INTO {T} (user_id, username, full_name, role, branch_code, branch_name, device_time, latitude, longitude,
                accuracy_m, located_at, outlet_latitude, outlet_longitude, radius_m, distance_m, outside_radius, low_accuracy,
                face_count, photo, photo_type, note, device, ip, risk_level, risk_signals, gps_samples)
            VALUES (%(u)s, %(un)s, %(fn)s, %(role)s, %(bc)s, %(bn)s, %(dt)s, %(lat)s, %(lng)s, %(acc)s, %(la)s, %(olat)s, %(olng)s,
                %(r)s, %(d)s, %(out)s, %(low)s, %(faces)s, %(photo)s, %(pt)s, %(note)s, %(dev)s, %(ip)s, %(rl)s, %(rs)s::jsonb,
                %(gs)s::jsonb)
            RETURNING id::text, visited_at""",
        {"u": user["id"], "un": user["username"], "fn": user.get("full_name"), "role": user["role"], "bc": branch["code"],
         "bn": branch["name"], "dt": device_time, "lat": lat, "lng": lng, "acc": accuracy, "la": located_at,
         "olat": branch["latitude"], "olng": branch["longitude"], "r": branch["radius_m"], "d": j["distance_m"],
         "out": j["outside_radius"], "low": j["low_accuracy"], "faces": faces, "photo": data, "pt": photo_type,
         "note": note, "dev": (device or "")[:300] or None, "ip": ip, "rl": risk["level"],
         "rs": json.dumps(risk["signals"], ensure_ascii=False), "gs": json.dumps((samples or [])[:30])})
    return {"id": row["id"], "visitedAt": row["visited_at"].isoformat(), "branchCode": branch["code"], "branchName": branch["name"],
            "faces": faces, "outletLocated": branch["latitude"] is not None, "radiusM": branch["radius_m"], **_camel(j),
            "riskLevel": risk["level"], "riskSignals": [s["code"] for s in risk["signals"]]}


def _camel(j: dict) -> dict:
    return {"distanceM": j["distance_m"], "outsideRadius": j["outside_radius"], "lowAccuracy": j["low_accuracy"]}


# ---------------------------------------------------------------- monitoring

COLUMNS = """c.id::text, c.user_id::text, c.username, c.full_name, c.role, c.branch_code, c.branch_name, c.visited_at,
    c.device_time, c.latitude, c.longitude, c.accuracy_m, c.located_at, c.outlet_latitude, c.outlet_longitude, c.radius_m,
    c.distance_m, c.outside_radius, c.low_accuracy, c.face_count, c.note, c.risk_level, c.risk_signals, c.review_status,
    c.review_note, c.reviewed_by, c.reviewed_at"""


def _view(r: dict, risk: bool = True) -> dict:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    extra = {"riskLevel": r.get("risk_level") or "none", "riskSignals": r.get("risk_signals") or [],
             "review": None if not r.get("review_status") else {
                 "status": r["review_status"], "note": r.get("review_note"), "by": r.get("reviewed_by"),
                 "at": r["reviewed_at"].isoformat() if r.get("reviewed_at") else None}} if risk else {}
    return {**extra,
        "id": r["id"], "userId": r["user_id"], "username": r["username"], "name": r["full_name"] or r["username"],
        "role": r["role"], "branchCode": r["branch_code"], "branchName": r["branch_name"],
        "visitedAt": r["visited_at"].isoformat(), "deviceTime": r["device_time"].isoformat() if r["device_time"] else None,
        "latitude": num(r["latitude"]), "longitude": num(r["longitude"]), "accuracyM": num(r["accuracy_m"]),
        "outletLatitude": num(r["outlet_latitude"]), "outletLongitude": num(r["outlet_longitude"]), "radiusM": r["radius_m"],
        "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"], "lowAccuracy": r["low_accuracy"],
        "outletLocated": r["outlet_latitude"] is not None, "faces": r["face_count"], "note": r["note"],
    }


def recent_of(user_id: str, limit: int = 10) -> list[dict]:
    # the PIC's own list: without the risk signals
    return [_view(r, risk=False) for r in db.fetch(
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
    elif location == "suspect":
        where.append("c.risk_level IN ('medium', 'high')")
    elif location == "alert":
        where.append("(c.outside_radius OR c.risk_level IN ('medium', 'high'))")
    elif location == "unreviewed":
        where.append("(c.outside_radius OR c.risk_level IN ('medium', 'high')) AND c.review_status IS NULL")
    cond = " AND ".join(where)
    rows = db.fetch(f"SELECT {COLUMNS} FROM {T} c WHERE {cond} ORDER BY c.visited_at DESC LIMIT %(lim)s", params)
    s = db.fetchrow(
        f"""SELECT count(*)::int AS total, count(DISTINCT c.user_id)::int AS pics, count(DISTINCT c.branch_code)::int AS branches,
                   count(*) FILTER (WHERE c.outside_radius)::int AS outside, count(*) FILTER (WHERE c.low_accuracy)::int AS low,
                   count(*) FILTER (WHERE c.outlet_latitude IS NULL)::int AS unlocated,
                   count(*) FILTER (WHERE NOT c.outside_radius AND c.outlet_latitude IS NOT NULL)::int AS inside,
                   count(*) FILTER (WHERE c.risk_level IN ('medium', 'high'))::int AS suspect,
                   count(*) FILTER (WHERE c.risk_level = 'high')::int AS suspect_high,
                   count(*) FILTER (WHERE (c.outside_radius OR c.risk_level IN ('medium', 'high'))
                                    AND c.review_status IS NULL)::int AS unreviewed
            FROM {T} c WHERE {cond}""", params)
    pics = db.fetch(
        f"""SELECT c.user_id::text AS id, COALESCE(max(c.full_name), max(c.username)) AS name, count(*)::int AS n
            FROM {T} c WHERE {cond} GROUP BY c.user_id ORDER BY 3 DESC, 2""", params)
    return {"data": [_view(r) for r in rows], "summary": s, "pics": pics, "truncated": len(rows) >= limit}


def photo_of(checkin_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT user_id::text, branch_code, photo, photo_type FROM {T} WHERE id::text = %s", (checkin_id,))


def review(checkin_id: str, status: str, note: Optional[str], user: dict, branches: Optional[tuple]) -> Optional[dict]:
    """A manager's verdict on a flagged check-in (valid / invalid); None when not found or out of scope."""
    row = db.fetchrow(
        f"""UPDATE {T} SET review_status = %(s)s, review_note = %(n)s, reviewed_by = %(by)s, reviewed_at = now()
            WHERE id::text = %(id)s AND (%(all)s OR branch_code = ANY(%(br)s))
            RETURNING id::text, branch_name, username""",
        {"s": status, "n": (note or "").strip()[:500] or None, "by": user.get("full_name") or user["username"],
         "id": checkin_id, "all": branches is None, "br": list(branches or [])})
    return row
