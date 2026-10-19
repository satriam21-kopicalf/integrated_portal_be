"""Rating & Review → Google Maps: the outlets on Google Maps through the official Places API (New), 2026-10-19.

Per Kopi Calf outlet (ESB master_branches, name "Kopi Calf …"): name, rating, number of reviews, address, business
status, opening hours (open / closed computed at read time, WIB) and the reviews the API gives (at most 5 per place,
chosen by Google: the full list needs the Business Profile API, see the portal guide).

Mapping outlet → Google place (integration_portal.gmaps_place, migration 020):
  * auto: a Text Search "<branch name>" biased to the outlet's coordinates (Cost Control → Lokasi Outlet) whose result
    is named "Kopi Calf …" and lies within AUTO_RADIUS_M of the outlet;
  * review: a "Kopi Calf" result farther away (or the outlet has no coordinates) that shares a word of the outlet's
    name (Kopi Calf To Go *Depok Beji*): shown, flagged to confirm;
  * missing: nothing found (searched again after a week);
  one place belongs to one outlet: when two outlets land on the same place, the better match keeps it (manual, auto,
  then the name, then the distance) and the other one searches again without the places already taken.
  * manual: picked by Cost Control / super admin in the portal (search → choose), or "none" = not on Google Maps.

Only the place id is kept for good; the place data is the latest fetch, replaced at every sync (no history), shown
with the Google attribution (Google Maps Platform terms).

    python -m app.gmaps                 # daily (cron): match the outlets without a place, refresh every place
    python -m app.gmaps --branch CCI01  # one outlet
    python -m app.gmaps --rematch       # search again the auto / review / missing matches
"""
import argparse
import json
import logging
import math
import re
import sys
import threading
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from app import database as db
from app.config import get_settings
from app.database import SCHEMA
from app.pic_checkin import distance_m, locations

logger = logging.getLogger("gmaps")

T = "integration_portal.gmaps_place"
API = "https://places.googleapis.com/v1"
WIB = ZoneInfo("Asia/Jakarta")
AUTO_RADIUS_M = 300
SEARCH_BIAS_M = 3000.0
RETRY_MISSING_DAYS = 7
TIMEOUT = 20
SEARCH_FIELDS = "places.id,places.displayName,places.formattedAddress,places.location"
DETAIL_FIELDS = ("id,displayName,formattedAddress,location,rating,userRatingCount,businessStatus,"
                 "regularOpeningHours.periods,regularOpeningHours.weekdayDescriptions,googleMapsUri,reviews")
STATUSES = ("auto", "review", "missing", "manual", "none")

_lock = threading.Lock()


class PlacesError(RuntimeError):
    """The Places API refused or failed (message for the portal)."""


def enabled() -> bool:
    return bool(get_settings().google_places_api_key)


def _call(method: str, path: str, fields: str, body: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    key = get_settings().google_places_api_key
    if not key:
        raise PlacesError("Google Places API key belum diatur di server (GOOGLE_PLACES_API_KEY)")
    try:
        r = requests.request(method, f"{API}/{path}", json=body, params=params, timeout=TIMEOUT,
                             headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields})
    except requests.RequestException as e:
        raise PlacesError(f"Google Places API tidak dapat dihubungi: {type(e).__name__}") from None
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        err = (data.get("error") or {})
        raise PlacesError(f"Google Places API {r.status_code} {err.get('status', '')}: {str(err.get('message', ''))[:200]}".strip())
    return data


# ---------------------------------------------------------------- outlets

def outlets() -> list[dict]:
    """The Kopi Calf outlets (active ESB branches) with their coordinates."""
    rows = db.fetch(
        f"""SELECT DISTINCT ON (branch_code) branch_code AS code, branch_name AS name FROM {SCHEMA}.master_branches
            WHERE COALESCE(branch_code, '') <> '' AND COALESCE(is_deleted, false) = false AND COALESCE(is_active, true)
              AND branch_name ILIKE 'Kopi Calf%%' ORDER BY branch_code, branch_name""")
    locs = locations([r["code"] for r in rows])
    return [{**r, "latitude": locs.get(r["code"], {}).get("latitude"), "longitude": locs.get(r["code"], {}).get("longitude")}
            for r in rows]


_STOP = {"kopi", "calf", "to", "go", "signature", "the", "coffee", "cafe", "outlet", "jl", "jalan"}


def _is_kopi_calf(name: str) -> bool:
    return "kopi calf" in " ".join((name or "").lower().replace("-", " ").split())


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _STOP and len(w) > 1}


def name_score(outlet_name: str, place_name: str) -> int:
    """Words the outlet's name and the place's name share, besides "Kopi Calf To Go"."""
    return len(_words(outlet_name) & _words(place_name))


def search(query: str, lat: Optional[float] = None, lng: Optional[float] = None, limit: int = 5) -> list[dict]:
    """Places for a text, nearest to the outlet first when it has coordinates."""
    body: dict[str, Any] = {"textQuery": query, "languageCode": "id", "regionCode": "ID", "maxResultCount": limit}
    if lat is not None and lng is not None:
        body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": SEARCH_BIAS_M}}
    out = []
    for p in _call("POST", "places:searchText", SEARCH_FIELDS, body=body).get("places", []):
        loc = p.get("location") or {}
        d = (distance_m(lat, lng, loc["latitude"], loc["longitude"])
             if lat is not None and lng is not None and "latitude" in loc else None)
        out.append({"placeId": p["id"], "name": (p.get("displayName") or {}).get("text", ""),
                    "address": p.get("formattedAddress", ""), "distanceM": d})
    return out


def match(outlet: dict, exclude: frozenset = frozenset()) -> dict:
    """{place_id, status, distance} for an outlet from a Text Search (see the module doc); `exclude`: places of other outlets."""
    found = [c for c in search(outlet["name"], outlet.get("latitude"), outlet.get("longitude"))
             if _is_kopi_calf(c["name"]) and c["placeId"] not in exclude]
    found.sort(key=lambda c: c["distanceM"] if c["distanceM"] is not None else math.inf)
    near = [c for c in found if c["distanceM"] is not None and c["distanceM"] <= AUTO_RADIUS_M]
    if near:
        return {"place_id": near[0]["placeId"], "status": "auto", "distance": near[0]["distanceM"]}
    # stable sort: the nearest first among the same score
    named = sorted((c for c in found if name_score(outlet["name"], c["name"])), key=lambda c: -name_score(outlet["name"], c["name"]))
    if named:
        return {"place_id": named[0]["placeId"], "status": "review", "distance": named[0]["distanceM"]}
    return {"place_id": None, "status": "missing", "distance": None}


def _rank(row: dict) -> tuple:
    """Which of the outlets on one place keeps it: manual, auto, the name, the distance."""
    name = ((row.get("data") or {}).get("displayName") or {}).get("text", "")
    d = row.get("match_distance_m")
    return (row.get("status") != "manual", row.get("status") != "auto", -name_score(row.get("branch_name", ""), name),
            d if d is not None else math.inf)


def details(place_id: str) -> dict:
    return _call("GET", f"places/{place_id}", DETAIL_FIELDS, params={"languageCode": "id"})


# ---------------------------------------------------------------- open / closed (WIB, from the regular hours)

def is_open(periods: Optional[list], now: Optional[datetime] = None) -> Optional[bool]:
    """Open at `now` per the regular opening periods (Places API: day 0 = Sunday); None = hours unknown."""
    if not periods:
        return None
    now = (now or datetime.now(WIB)).astimezone(WIB)
    week = 7 * 1440
    t = ((now.weekday() + 1) % 7) * 1440 + now.hour * 60 + now.minute
    for p in periods:
        o = p.get("open") or {}
        if "close" not in p:  # open 24 hours
            return True
        c = p["close"]
        start = o.get("day", 0) * 1440 + o.get("hour", 0) * 60 + o.get("minute", 0)
        end = c.get("day", 0) * 1440 + c.get("hour", 0) * 60 + c.get("minute", 0)
        if end <= start:
            end += week
        if start <= t < end or start <= t + week < end:
            return True
    return False


def today_hours(descriptions: Optional[list], now: Optional[datetime] = None) -> Optional[str]:
    """Today's line of the weekday descriptions (Monday first), without the day name."""
    if not descriptions or len(descriptions) < 7:
        return None
    line = descriptions[(now or datetime.now(WIB)).astimezone(WIB).weekday()]
    return line.split(": ", 1)[1] if ": " in line else line


# ---------------------------------------------------------------- store

def _rows(codes: Optional[list[str]] = None) -> dict[str, dict]:
    rows = db.fetch(f"SELECT * FROM {T}" + (" WHERE branch_code = ANY(%(c)s)" if codes is not None else ""), {"c": codes})
    return {r["branch_code"]: r for r in rows}


def _save(code: str, name: str, **cols: Any) -> None:
    cols = {k: (json.dumps(v, ensure_ascii=False) if k in ("data",) and v is not None else v) for k, v in cols.items()}
    names = ["branch_code", "branch_name", *cols]
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in names[1:])
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO {T} ({', '.join(names)}, updated_at) VALUES ({', '.join(['%s'] * len(names))}, now()) "
            f"ON CONFLICT (branch_code) DO UPDATE SET {sets}, updated_at = now()",
            (code, name, *cols.values()))


def set_place(code: str, place_id: Optional[str], user: dict) -> dict:
    """Manual choice of the place of an outlet (None = not on Google Maps); fetches it right away."""
    outlet = next((o for o in outlets() if o["code"] == code), None)
    if not outlet:
        raise PlacesError("Outlet tidak ditemukan")
    if place_id is None:
        _save(code, outlet["name"], place_id=None, status="none", match_distance_m=None, set_by=user.get("username"),
              matched_at=datetime.now(WIB), data=None, fetched_at=None, error=None)
        return {"status": "none"}
    d = details(place_id)
    loc = d.get("location") or {}
    dist = (distance_m(outlet["latitude"], outlet["longitude"], loc["latitude"], loc["longitude"])
            if outlet.get("latitude") is not None and "latitude" in loc else None)
    _save(code, outlet["name"], place_id=d.get("id", place_id), status="manual", match_distance_m=dist,
          set_by=user.get("username"), matched_at=datetime.now(WIB), data=d, fetched_at=datetime.now(WIB), error=None)
    return {"status": "manual", "distanceM": dist}


def sync(branch: Optional[str] = None, rematch: bool = False) -> dict:
    """Match the outlets without a place and refresh every place (one run at a time)."""
    if not _lock.acquire(blocking=False):
        raise PlacesError("Sinkronisasi Google Maps sedang berjalan")
    try:
        todo = [o for o in outlets() if branch is None or o["code"] == branch]
        known = _rows([o["code"] for o in todo])
        now = datetime.now(WIB)
        stats = {"outlets": len(todo), "matched": 0, "refreshed": 0, "missing": 0, "failed": 0}
        by_code = {o["code"]: o for o in todo}

        def matched(o: dict, exclude: frozenset = frozenset()) -> None:
            m = match(o, exclude)
            _save(o["code"], o["name"], place_id=m["place_id"], status=m["status"], match_distance_m=m["distance"],
                  matched_at=now, error=None, data=None, fetched_at=None)
            stats["matched"] += bool(m["place_id"])

        def guarded(o: dict, job) -> bool:
            try:
                job()
                return True
            except PlacesError as e:
                logger.warning("%s %s: %s", o["code"], o["name"], e)
                _save(o["code"], o["name"], error=str(e)[:300])
                stats["failed"] += 1
                if " 403 " in f" {e} " or "API key" in str(e):  # the key itself is refused: stop, every call would fail
                    raise
                return False

        try:
            # 1. outlets without a place (all of them with --rematch)
            for o in todo:
                row = known.get(o["code"]) or {}
                status, place = row.get("status"), row.get("place_id")
                stale = row.get("matched_at") is None or (now - row["matched_at"]).days >= RETRY_MISSING_DAYS
                if status not in ("manual", "none") and (not place or rematch) and (status != "missing" or stale or rematch):
                    guarded(o, lambda o=o: matched(o))
            # 2. one place, one outlet: the others search again without the places already taken
            owners: dict[str, list[dict]] = {}
            for r in _rows().values():
                if r.get("place_id"):
                    owners.setdefault(r["place_id"], []).append(r)
            taken = frozenset(owners)
            for group in owners.values():
                for loser in sorted(group, key=_rank)[1:]:
                    o = by_code.get(loser["branch_code"])
                    if o and loser.get("status") != "manual":
                        guarded(o, lambda o=o: matched(o, taken))
            # 3. refresh every place
            for code, r in _rows(list(by_code)).items():
                if r.get("place_id") and code in by_code:
                    o = by_code[code]
                    if guarded(o, lambda o=o, r=r: _save(o["code"], o["name"], data=details(r["place_id"]),
                                                       fetched_at=datetime.now(WIB), error=None)):
                        stats["refreshed"] += 1
        except PlacesError:
            pass  # the key is refused: logged, the rest waits for the next run
        stats["missing"] = sum(1 for r in _rows(list(by_code)).values() if r.get("status") == "missing")
        return stats
    finally:
        _lock.release()


def sync_in_background(branch: Optional[str] = None) -> None:
    def run() -> None:
        try:
            logger.info("gmaps sync: %s", sync(branch))
        except PlacesError as e:
            logger.warning("gmaps sync: %s", e)
        except Exception:  # noqa: BLE001 - background thread
            logger.exception("gmaps sync failed")
    threading.Thread(target=run, name="gmaps-sync", daemon=True).start()


def running() -> bool:
    return _lock.locked()


# ---------------------------------------------------------------- read

def _review(r: dict) -> dict:
    a = r.get("authorAttribution") or {}
    return {"rating": r.get("rating"), "text": (r.get("text") or r.get("originalText") or {}).get("text", ""),
            "when": r.get("relativePublishTimeDescription"), "publishedAt": r.get("publishTime"),
            "author": a.get("displayName"), "authorUri": a.get("uri"), "authorPhoto": a.get("photoUri"),
            "uri": r.get("googleMapsUri")}


def overview(codes: Optional[tuple[str, ...]] = None) -> list[dict]:
    """Every outlet in scope with its place, the latest data and the open / closed state now."""
    now = datetime.now(WIB)
    todo = [o for o in outlets() if codes is None or o["code"] in codes]
    rows = _rows([o["code"] for o in todo])
    out = []
    for o in todo:
        r = rows.get(o["code"]) or {}
        d = r.get("data") or {}
        hours = d.get("regularOpeningHours") or {}
        business = d.get("businessStatus")
        out.append({
            "code": o["code"], "branchName": o["name"], "located": o.get("latitude") is not None,
            "placeId": r.get("place_id"), "match": r.get("status") or "pending", "matchDistanceM": r.get("match_distance_m"),
            "setBy": r.get("set_by"), "name": (d.get("displayName") or {}).get("text"), "address": d.get("formattedAddress"),
            "rating": d.get("rating"), "reviewCount": d.get("userRatingCount"), "businessStatus": business,
            "openNow": None if business and business != "OPERATIONAL" else is_open(hours.get("periods"), now),
            "todayHours": today_hours(hours.get("weekdayDescriptions"), now), "weekHours": hours.get("weekdayDescriptions") or [],
            # the reviews Google gives (it has no "newest" order in the Places API New), newest first
            "mapsUri": d.get("googleMapsUri"),
            "reviews": sorted((_review(x) for x in d.get("reviews") or []), key=lambda x: x["publishedAt"] or "", reverse=True),
            "fetchedAt": r["fetched_at"].isoformat() if r.get("fetched_at") else None, "error": r.get("error"),
        })
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--branch")
    ap.add_argument("--rematch", action="store_true")
    a = ap.parse_args()
    if not enabled():
        logger.info("GOOGLE_PLACES_API_KEY not set - skipped")
        return 0
    try:
        logger.info("done: %s", sync(a.branch, a.rematch))
    except PlacesError as e:
        logger.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
