"""Form → Monitoring Product / Monitoring Outlet and their Monitoring pages (2026-10-20), from the workbook
"Form Visit" (integrated_portal docs): Monitoring Product = AVAILIBILITY to KUALITAS PRODUK, Monitoring Outlet =
KEBERSIHAN DAN RATING OUTLET to KENYAMANAN OUTLET, plus ADMINISTRASI & FINANCE (added 2026-10-21); the overall
performance and the signatures of the workbook are not part of these forms.

A PIC visits one outlet at a time: the visit is a draft (saved as it is filled, continued later) for the signed-in
account and the branch picked (a PIC with several branches fills one visit per branch), then submitted. Every item is
rated UNDER / MEET / EXCEED with a note and camera photos (stamped in the browser with the time, the branch and the
position; the server keeps the position and its distance to the outlet). Every rated item needs a note and at least one
photo — material for the analysis (2026-10-23; before: UNDER only); items left unrated do not block the sending.
Daily (2026-10-23): one visit per PIC, form and branch per day (WIB) — the next one the day after; a day without any
visit of a PIC counts as a day off in the monitoring (attendance).
Tables integration_portal.visit_form and visit_form_photo (migration 023).
"""
import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError, branch_options, judge, locations, parse_photo

T = "integration_portal.visit_form"
TP = "integration_portal.visit_form_photo"
RATINGS = ("UNDER", "MEET", "EXCEED")
MAX_PHOTOS_PER_ITEM = 3


class VisitError(CheckInError):
    """A visit refused with a message for the PIC (HTTP 422)."""


def _items(prefix: str, labels: list[str]) -> list[dict]:
    return [{"key": f"{prefix}.{i}", "no": i, "label": label, "kind": "rating"} for i, label in enumerate(labels, start=1)]


PREMIX = ["Premix Premium", "Premix Regular", "Premix Premium Oat", "Premix Smooth", "Premix Magic", "Premix Jefferson",
          "Premix Coklat", "Premix Tea Series", "Premix Matcha", "Premix Taro", "Premix Dirty & Ice White", "Colbrew Liquid",
          "Cream Based", "Creamer Liquid", "Dimsum", "Roti Kukus", "Puding", "Side Dish Lainnya", "Maincourse"]

DEFINITIONS: dict[str, dict] = {
    "product": {
        "title": "Monitoring Product",
        "groups": [
            {"key": "availability", "title": "Availability", "subtitle": "Ketersediaan dan Kesesuaian Produk", "sections": [
                {"key": "stok-preparation", "title": "Stok Preparation", "items": _items("stok-preparation", [f"Ketersediaan Stok {x}" for x in PREMIX])},
                {"key": "shelf-life", "title": "Shelf Life Label", "items": _items("shelf-life", [f"Shelf Life Label {x}" for x in PREMIX])},
                {"key": "espresso-beans", "title": "Espresso dan Beans", "items": _items("espresso-beans", [
                    "Ketersediaan Stok Espresso Premium", "Ketersediaan Stok Espresso Regular", "Ketersediaan Stok Beans Premium",
                    "Ketersediaan Stok Beans Regular", "Ketersediaan Stok Beans Cold Brew", "Ketersediaan Stok Beans Specialty"])},
                {"key": "bahan-penolong", "title": "Bahan Penolong", "items": _items("bahan-penolong", [
                    "Ketersediaan Cup Es", "Ketersediaan Cup Frosted", "Ketersediaan Cup Jefferson", "Ketersediaan Paper Cup",
                    "Ketersediaan Es Batu", "Ketersediaan Es Batu dalam Klip", "Ketersediaan Can Dan Tutup Can (Sudah Stiker)",
                    "Ketersediaan Botol Dan Tutup Botol (Sudah Stiker)", "Ketersediaan Straw 5mm", "Ketersediaan Straw 6mm",
                    "Ketersediaan Lunch Box S", "Ketersediaan Lunch Box M", "Ketersediaan Set Spork", "Ketersediaan Greeting Card",
                    "Ketersediaan Beans Card Specialty", "Ketersediaan Plastik Take Away"])},
                {"key": "bahan-baku", "title": "Bahan Baku", "items": _items("bahan-baku", [
                    "Kesesuaian Form Request Bahan Baku", "Kesesuaian Form Request FM, Oat Milk, Ice Cream, Brookfarm",
                    "Kesesuaian Form Request Ice Hot, Rich Gold", "Kesesuaian Form Request Bahan Baku Maincourse",
                    "Kesesuaian Form Request Bahan Baku Side Dishes"])},
            ]},
            {"key": "kualitas-produk", "title": "Kualitas Produk", "subtitle": "5 Senses - Taste", "sections": [
                {"key": "kualitas-produk", "title": "Kualitas Produk", "items": _items("kualitas-produk", [
                    "Cupping Premium", "Cupping Regular", "Cupping All Premix", "Cupping All Cream & Foam", "Cupping Menu Specialty",
                    "Test Food WIP Side Dish & Main Course", "Test Food Menu Side Dish", "Test Food Menu Main Course",
                    "Handling & Treatment Product"])},
            ]},
        ],
    },
    "outlet": {
        "title": "Monitoring Outlet",
        "groups": [
            {"key": "kebersihan-rating", "title": "Kebersihan dan Rating Outlet", "subtitle": "5 Senses - Eye", "sections": [
                {"key": "kebersihan", "title": "Kebersihan Outlet", "items": _items("kebersihan", [
                    "Kebersihan Area Bar Bersih", "Kebersihan Area Bar Kotor", "Kebersihan Area Kitchen", "Kebersihan Tools",
                    "Kebersihan Area Facade / Halaman Depan", "Kebersihan Area Indoor", "Kebersihan Area Outdoor",
                    "Kebersihan Area Toilet & Musholla", "Kebersihan Area Warehouse", "Kebersihan Area Mess"])},
                # platform = the stars of the outlet on that platform go with the item (2026-10-21): Google from
                # Rating & Review → Google Maps (automatic), the delivery apps typed by the PIC (no API yet)
                {"key": "rating", "title": "Rating Outlet", "items": [
                    {**it, **({"platform": p} if p else {})} for it, p in zip(_items("rating", [
                        "Rating Google", "Rating Gofood", "Rating Grabfood", "Rating Shopeefood", "Complain Customer"]),
                        ("google", "gofood", "grabfood", "shopeefood", None))]},
            ]},
            {"key": "barista-flow", "title": "Barista + Flow Bar", "subtitle": "Memastikan Role and Responsibilities di dalam Bar", "sections": [
                {"key": "barista", "title": "Barista", "items": [
                    {"key": "barista.1", "no": 1, "label": "Grooming Barista", "kind": "rating"},
                    {"key": "barista.2", "no": 2, "label": "Kedisiplinan Barista", "kind": "rating"},
                    {"key": "barista.3", "no": 3, "label": "Nama Barista Telat", "kind": "text"},
                    {"key": "barista.4", "no": 4, "label": "Update Rekap Absen Barista", "kind": "rating"}]},
                {"key": "flow-bar", "title": "Flow Bar", "items": _items("flow-bar", [
                    "Speed Of Service", "Kesesuaian Role Barista", "Kesesuaian Flow Bar"])},
            ]},
            {"key": "hospitality", "title": "Hospitality", "subtitle": "5 Senses - Ear - Produk Knowledge", "sections": [
                {"key": "hospitality", "title": "Hospitality", "items": _items("hospitality", [
                    "Product Knowledge Barista", "Greetings Barista", "Offering Product Barista", "Music"])},
            ]},
            {"key": "smell", "title": "Smell", "subtitle": "5 Senses - Wangi Outlet - Alat alat Kebersihan", "sections": [
                {"key": "alat-kebersihan", "title": "Kelengkapan Alat Kebersihan", "items": _items("alat-kebersihan", [
                    "Sapu", "Pengki", "Pel Area Bar", "Pel Area Customer", "Wipper", "Pel Karet", "Sikat Mesin Kopi & Grinder",
                    "Lap Area Bar Depan", "Lap Area Kitchen & Bar Belakang", "Lap Area Customer", "Lap Gelas", "Lap Tools",
                    "Lap Mesin Kopi", "Lap Steam Milk", "Lap Basket Porta"])},
                {"key": "chemical", "title": "Kelengkapan Chemical", "items": _items("chemical", [
                    "Sabun Kaca", "Sabun Cuci Piring", "Sabun Cuci Tangan", "Sabun Lantai", "Sabun Toilet", "Pengharum AC",
                    "Pengharum Ruangan", "Pengharum Mushola & Toilet"])},
            ]},
            {"key": "kenyamanan", "title": "Kenyamanan Outlet", "subtitle": "5 Senses - Suhu Ruangan & Kenyamanan Outlet", "sections": [
                {"key": "kenyamanan", "title": "Kenyamanan Outlet", "items": _items("kenyamanan", [
                    "Kondisi Ruangan", "Kondisi Furniture (Sofa, Meja, Karpet, Kursi)", "Kondisi Internet Customer",
                    "Kondisi Merchandise"])},
            ]},
            # added 2026-10-21 (user request): the workbook's ADMINISTRASI & FINANCE
            {"key": "administrasi-finance", "title": "Administrasi & Finance", "subtitle": "Administrasi & Finance Outlet", "sections": [
                {"key": "administrasi-finance", "title": "Administrasi & Finance", "items": _items("administrasi-finance", [
                    "SO Packaging", "SO Weekly / Monthly", "Daily Cupping", "Dokumentasi Surat Jalan", "Form Selfpickup",
                    "Penggunaan Pitty Cash Terkontrol", "Setoran Uang Pembelian Menggunakan Cash"])},
            ]},
        ],
    },
}


def all_items(form: str) -> list[dict]:
    """Every item of a form with its group and section."""
    out = []
    for g in DEFINITIONS[form]["groups"]:
        for s in g["sections"]:
            for it in s["items"]:
                out.append({**it, "group": g["key"], "section": s["key"], "section_title": s["title"]})
    return out


def _counts(form: str, answers: dict) -> dict:
    rated = [answers.get(it["key"], {}).get("rating") for it in all_items(form) if it["kind"] == "rating"]
    c = {r: sum(1 for x in rated if x == r) for r in RATINGS}
    done = sum(c.values())
    return {"under": c["UNDER"], "meet": c["MEET"], "exceed": c["EXCEED"], "rated": done, "total": len(rated),
            "score": round((c["MEET"] + c["EXCEED"]) * 100 / done, 1) if done else None}


# ---------------------------------------------------------------- visits

COLS = """v.id::text, v.form, v.status, v.user_id::text, v.username, v.full_name, v.branch_code, v.branch_name, v.started_at,
    v.updated_at, v.submitted_at, v.answers, v.counts, v.score, v.latitude, v.longitude, v.accuracy_m, v.outlet_latitude,
    v.outlet_longitude, v.radius_m, v.distance_m, v.outside_radius, v.low_accuracy, v.signed_name, v.signed_at,
    (v.signature IS NOT NULL) AS has_signature"""


def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def photos_of(visit_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id::text, item_key, taken_at, uploaded_at, latitude, longitude, distance_m, outside_radius
                        FROM {TP} WHERE visit_id::text = %s ORDER BY uploaded_at""", (visit_id,))
    return [{"id": r["id"], "item": r["item_key"], "takenAt": r["taken_at"].isoformat() if r["taken_at"] else None,
             "uploadedAt": r["uploaded_at"].isoformat(), "latitude": _num(r["latitude"]), "longitude": _num(r["longitude"]),
             "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"]} for r in rows]


def _view(r: dict, photos: Optional[list] = None) -> dict:
    out = {
        "id": r["id"], "form": r["form"], "status": r["status"], "userId": r["user_id"], "username": r["username"],
        "name": r["full_name"] or r["username"], "branchCode": r["branch_code"], "branchName": r["branch_name"],
        "startedAt": r["started_at"].isoformat(), "updatedAt": r["updated_at"].isoformat(),
        "submittedAt": r["submitted_at"].isoformat() if r["submitted_at"] else None,
        "counts": r["counts"] or _counts(r["form"], r["answers"] or {}), "score": _num(r["score"]),
        "latitude": _num(r["latitude"]), "longitude": _num(r["longitude"]), "accuracyM": _num(r["accuracy_m"]),
        "outletLatitude": _num(r["outlet_latitude"]), "outletLongitude": _num(r["outlet_longitude"]), "radiusM": r["radius_m"],
        "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"], "lowAccuracy": r["low_accuracy"],
        "signature": {"name": r["signed_name"], "signedAt": r["signed_at"].isoformat() if r["signed_at"] else None}
        if r["has_signature"] else None,
    }
    if photos is not None:
        out["answers"] = r["answers"] or {}
        out["photos"] = photos
    return out


def get_visit(visit_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {COLS} FROM {T} v WHERE v.id::text = %s", (visit_id,))


def google_rating(branch_code: str) -> Optional[dict]:
    """The outlet's latest Google Maps rating (Rating & Review → Google Maps, app/gmaps.py); None when not on Google."""
    r = db.fetchrow("SELECT data, fetched_at FROM integration_portal.gmaps_place WHERE branch_code = %s AND place_id IS NOT NULL",
                    (branch_code,))
    d = (r or {}).get("data") or {}
    if d.get("rating") is None:
        return None
    return {"stars": float(d["rating"]), "reviews": d.get("userRatingCount"), "source": "google",
            "fetchedAt": r["fetched_at"].isoformat() if r.get("fetched_at") else None, "mapsUri": d.get("googleMapsUri")}


def _auto_items(form: str) -> list[dict]:
    return [it for it in all_items(form) if it.get("platform") == "google"]


def full(visit_id: str) -> dict:
    row = get_visit(visit_id)
    out = _view(row, photos_of(visit_id))
    if row["status"] == "DRAFT" and _auto_items(row["form"]):
        # a draft shows the current figure; it is kept in the answers at sending
        g = google_rating(row["branch_code"])
        out["auto"] = {it["key"]: g for it in _auto_items(row["form"])} if g else {}
    return out


def done_today(form: str, user_id: str) -> dict[str, dict]:
    """{branch code: {id, submittedAt}} of the PIC's visits sent today (WIB)."""
    rows = db.fetch(f"""SELECT DISTINCT ON (branch_code) branch_code, id::text, submitted_at FROM {T}
                        WHERE form = %s AND user_id::text = %s AND status = 'SUBMITTED'
                          AND (submitted_at AT TIME ZONE 'Asia/Jakarta')::date = (now() AT TIME ZONE 'Asia/Jakarta')::date
                        ORDER BY branch_code, submitted_at""", (form, user_id))
    return {r["branch_code"]: {"id": r["id"], "submittedAt": r["submitted_at"].isoformat()} for r in rows}


def _not_today(form: str, user: dict, branch_code: str, branch_name: str) -> None:
    if done_today(form, str(user["id"])).get(branch_code):
        raise VisitError(f"Form untuk {branch_name} sudah dikirim hari ini. Pengisian berikutnya besok.")


def form_state(form: str, user: dict) -> dict:
    """What the form page needs: the account, its branches, its drafts, its latest visits and the branches done today."""
    branches = branch_options(user)
    drafts = db.fetch(f"SELECT {COLS} FROM {T} v WHERE v.form = %s AND v.user_id::text = %s AND v.status = 'DRAFT' ORDER BY v.updated_at DESC",
                      (form, str(user["id"])))
    recent = db.fetch(f"""SELECT {COLS} FROM {T} v WHERE v.form = %s AND v.user_id::text = %s AND v.status = 'SUBMITTED'
                         ORDER BY v.submitted_at DESC LIMIT 10""", (form, str(user["id"])))
    return {"definition": DEFINITIONS[form], "user": {"id": str(user["id"]), "username": user["username"],
            "name": user.get("full_name") or user["username"]}, "branches": branches,
            "drafts": [_view(r) for r in drafts], "recent": [_view(r) for r in recent], "today": done_today(form, str(user["id"]))}


def open_visit(form: str, user: dict, branch_code: str) -> dict:
    """The user's draft for the branch, started when there is none."""
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise VisitError("Cabang tidak tersedia untuk akun Anda")
    _not_today(form, user, branch["code"], branch["name"])
    row = db.fetchrow(f"""SELECT id::text FROM {T} WHERE form = %s AND user_id::text = %s AND branch_code = %s AND status = 'DRAFT'""",
                      (form, str(user["id"]), branch["code"]))
    if row is None:
        row = db.fetchrow(
            f"""INSERT INTO {T} (form, user_id, username, full_name, role, branch_code, branch_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (form, user_id, branch_code) WHERE status = 'DRAFT' DO UPDATE SET updated_at = now()
                RETURNING id::text""",
            (form, user["id"], user["username"], user.get("full_name"), user["role"], branch["code"], branch["name"]))
    return full(row["id"])


def own_draft(form: str, user: dict, visit_id: str) -> dict:
    row = get_visit(visit_id)
    if row is None or row["form"] != form or row["user_id"] != str(user["id"]):
        raise LookupError("Kunjungan tidak ditemukan")
    if row["status"] != "DRAFT":
        raise VisitError("Kunjungan ini sudah dikirim")
    return row


def save_answers(form: str, user: dict, visit_id: str, patch: dict) -> dict:
    """Merges {item: {rating?, note?}} into the draft's answers."""
    row = own_draft(form, user, visit_id)
    known = {it["key"]: it for it in all_items(form)}
    answers = dict(row["answers"] or {})
    for key, value in patch.items():
        if key not in known or not isinstance(value, dict):
            raise VisitError(f"Butir tidak dikenal: {key}")
        cur = dict(answers.get(key) or {})
        if "rating" in value:
            rating = value["rating"]
            if rating is not None and (known[key]["kind"] != "rating" or rating not in RATINGS):
                raise VisitError("Penilaian harus UNDER, MEET atau EXCEED")
            cur["rating"] = rating
        if "note" in value:
            cur["note"] = (str(value["note"] or "")).strip()[:1000] or None
        if "stars" in value:
            # typed by the PIC for the delivery apps; Google comes from Google Maps
            if not known[key].get("platform") or known[key]["platform"] == "google":
                raise VisitError("Rating bintang hanya untuk GoFood, GrabFood dan ShopeeFood")
            stars = value["stars"]
            if stars is not None:
                try:
                    stars = round(float(stars), 1)
                except (TypeError, ValueError):
                    raise VisitError("Rating harus angka 1,0 sampai 5,0") from None
                if not 1 <= stars <= 5:
                    raise VisitError("Rating harus angka 1,0 sampai 5,0")
            cur["stars"] = stars
            cur["source"] = "manual" if stars is not None else None
        answers[key] = {k: v for k, v in cur.items() if v is not None}
    answers = {k: v for k, v in answers.items() if v}
    counts = _counts(form, answers)
    db.fetchrow(f"""UPDATE {T} SET answers = %s::jsonb, counts = %s::jsonb, score = %s, updated_at = now()
                    WHERE id::text = %s RETURNING id""", (json.dumps(answers), json.dumps(counts), counts["score"], visit_id))
    return {"answers": answers, "counts": counts}


def _outlet(branch_code: str) -> Optional[dict]:
    return locations([branch_code]).get(branch_code)


def add_photo(form: str, user: dict, visit_id: str, item: str, photo: str, lat: Optional[float], lng: Optional[float],
              taken_at: Optional[datetime]) -> dict:
    row = own_draft(form, user, visit_id)
    if item not in {it["key"] for it in all_items(form)}:
        raise VisitError(f"Butir tidak dikenal: {item}")
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {TP} WHERE visit_id::text = %s AND item_key = %s", (visit_id, item))["n"]
    if n >= MAX_PHOTOS_PER_ITEM:
        raise VisitError(f"Maksimal {MAX_PHOTOS_PER_ITEM} foto per butir; hapus satu dulu")
    photo_type, data = parse_photo(photo)
    j = judge(lat, lng, None, _outlet(row["branch_code"])) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    db.fetchrow(
        f"""INSERT INTO {TP} (visit_id, item_key, photo, photo_type, taken_at, latitude, longitude, distance_m, outside_radius, uploaded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (visit_id, item, data, photo_type, taken_at, lat, lng, j["distance_m"], j["outside_radius"], user["id"]))
    db.fetchrow(f"UPDATE {T} SET updated_at = now() WHERE id::text = %s RETURNING id", (visit_id,))
    return {"photos": photos_of(visit_id)}


def delete_photo(form: str, user: dict, visit_id: str, photo_id: str) -> dict:
    own_draft(form, user, visit_id)
    db.fetchrow(f"DELETE FROM {TP} WHERE id::text = %s AND visit_id::text = %s RETURNING id", (photo_id, visit_id))
    return {"photos": photos_of(visit_id)}


def unrated(form: str, answers: dict) -> int:
    return sum(1 for it in all_items(form) if it["kind"] == "rating" and not (answers.get(it["key"]) or {}).get("rating"))


def missing(form: str, answers: dict, photos: list[dict]) -> list[dict]:
    """What blocks the submit: rated items without a note or a photo (unrated items are allowed, 2026-10-21)."""
    with_photo = {p["item"] for p in photos}
    out = []
    for it in all_items(form):
        if it["kind"] != "rating":
            continue
        a = answers.get(it["key"]) or {}
        if a.get("rating") and not it.get("platform"):  # the platform stars (Rating Outlet) need neither
            need = [x for x, ok in (("note", bool(a.get("note"))), ("photo", it["key"] in with_photo)) if not ok]
            if need:
                out.append({"key": it["key"], "label": it["label"], "need": "+".join(need)})
    return out


def submit(form: str, user: dict, visit_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float],
           device: Optional[str], ip: Optional[str], signature: Optional[str] = None, signed_name: Optional[str] = None) -> dict:
    row = own_draft(form, user, visit_id)
    _not_today(form, user, row["branch_code"], row["branch_name"])
    photos = photos_of(visit_id)
    todo = missing(form, row["answers"] or {}, photos)
    if todo:
        raise VisitError(f"Belum bisa dikirim: {len(todo)} butir yang dinilai belum lengkap catatan/foto")
    if unrated(form, row["answers"] or {}) == len([i for i in all_items(form) if i["kind"] == "rating"]):
        raise VisitError("Belum ada butir yang dinilai")
    # the signature is optional (like the stock opname); a name goes with it
    sig_type, sig = parse_photo(signature) if signature else (None, None)
    signed_name = " ".join((signed_name or "").split())[:120] or None
    if sig is not None and not signed_name:
        raise VisitError("Tulis nama penanda tangan di bawah tanda tangan")
    outlet = _outlet(row["branch_code"]) or {}
    j = judge(lat, lng, accuracy, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False, "low_accuracy": False}
    answers = dict(row["answers"] or {})
    auto = _auto_items(form)
    g = google_rating(row["branch_code"]) if auto else None
    for it in auto:
        # the Google rating of the moment of sending stays with the visit
        cur = {k: v for k, v in (answers.get(it["key"]) or {}).items() if k not in ("stars", "reviews", "source", "fetchedAt")}
        answers[it["key"]] = {**cur, **({k: g[k] for k in ("stars", "reviews", "source", "fetchedAt")} if g else {})}
        if not answers[it["key"]]:
            answers.pop(it["key"])
    counts = _counts(form, answers)
    db.fetchrow(
        f"""UPDATE {T} SET status = 'SUBMITTED', submitted_at = now(), updated_at = now(), answers = %s::jsonb, counts = %s::jsonb, score = %s,
                latitude = %s, longitude = %s, accuracy_m = %s, outlet_latitude = %s, outlet_longitude = %s, radius_m = %s,
                distance_m = %s, outside_radius = %s, low_accuracy = %s, device = %s, ip = %s,
                signature = %s, signature_type = %s, signed_name = %s, signed_at = CASE WHEN %s THEN now() END
            WHERE id::text = %s RETURNING id""",
        (json.dumps(answers), json.dumps(counts), counts["score"], lat, lng, accuracy, outlet.get("latitude"), outlet.get("longitude"),
         outlet.get("radius_m"), j["distance_m"], j["outside_radius"], j.get("low_accuracy", False), (device or "")[:300] or None,
         ip, sig, sig_type, signed_name if sig is not None else None, sig is not None, visit_id))
    return full(visit_id)


def discard(form: str, user: dict, visit_id: str) -> None:
    own_draft(form, user, visit_id)
    db.fetchrow(f"DELETE FROM {T} WHERE id::text = %s AND status = 'DRAFT' RETURNING id", (visit_id,))


# ---------------------------------------------------------------- monitoring

def monitor(form: str, date_from: date, date_to: date, branches: Optional[list[str]], user_id: Optional[str] = None,
            limit: int = 1000) -> dict:
    """Submitted visits of a period (WIB days) with the figures, the performance per section and the most frequent
    findings (items rated UNDER)."""
    where = ["v.form = %(f)s", "v.status = 'SUBMITTED'", "(v.submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"f": form, "a": date_from, "b": date_to, "lim": limit}
    if branches is not None:
        where.append("v.branch_code = ANY(%(br)s)")
        params["br"] = branches
    if user_id:
        where.append("v.user_id::text = %(u)s")
        params["u"] = user_id
    cond = " AND ".join(where)
    rows = db.fetch(f"""SELECT {COLS}, (SELECT count(*)::int FROM {TP} p WHERE p.visit_id = v.id) AS photo_count
                        FROM {T} v WHERE {cond} ORDER BY v.submitted_at DESC LIMIT %(lim)s""", params)
    items = all_items(form)
    sections: dict[str, dict] = {}
    findings: dict[str, dict] = {}
    for r in rows:
        answers = r["answers"] or {}
        for it in items:
            rating = (answers.get(it["key"]) or {}).get("rating")
            if it["kind"] != "rating" or not rating:
                continue
            s = sections.setdefault(it["section"], {"key": it["section"], "title": it["section_title"], "group": it["group"],
                                                    "under": 0, "meet": 0, "exceed": 0})
            s[rating.lower()] += 1
            if rating == "UNDER":
                f = findings.setdefault(it["key"], {"key": it["key"], "label": it["label"], "section": it["section_title"],
                                                    "count": 0, "branches": set()})
                f["count"] += 1
                f["branches"].add(r["branch_name"])
    data = []
    for r in rows:
        v = _view(r)
        v["photoCount"] = r["photo_count"]
        data.append(v)
    scores = [v["score"] for v in data if v["score"] is not None]
    summary = {"visits": len(data), "branches": len({v["branchCode"] for v in data}), "pics": len({v["userId"] for v in data}),
               "under": sum(v["counts"]["under"] for v in data), "meet": sum(v["counts"]["meet"] for v in data),
               "exceed": sum(v["counts"]["exceed"] for v in data),
               "score": round(sum(scores) / len(scores), 1) if scores else None,
               "outside": sum(1 for v in data if v["outsideRadius"])}
    order = {s["key"]: i for i, s in enumerate(s for g in DEFINITIONS[form]["groups"] for s in g["sections"])}
    top = sorted(findings.values(), key=lambda f: (-f["count"], f["label"]))[:15]
    pics = {}
    for v in data:
        pics.setdefault(v["userId"], {"id": v["userId"], "name": v["name"], "n": 0})["n"] += 1
    # stars per platform (Rating Outlet): average of the visits that carry one
    platforms = []
    for it in items:
        if it.get("platform"):
            vals = [float(a["stars"]) for r in rows if (a := (r["answers"] or {}).get(it["key"]) or {}).get("stars") is not None]
            platforms.append({"key": it["key"], "platform": it["platform"], "label": it["label"],
                              "stars": round(sum(vals) / len(vals), 2) if vals else None, "visits": len(vals)})
    return {"definition": DEFINITIONS[form], "data": data, "summary": summary, "platforms": platforms,
            "attendance": attendance(form, date_from, date_to, branches),
            "sections": sorted(sections.values(), key=lambda s: order.get(s["key"], 99)),
            "findings": [{**f, "branches": sorted(f["branches"])} for f in top],
            "pics": sorted(pics.values(), key=lambda p: -p["n"]), "truncated": len(rows) >= limit}


def signature_of(visit_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT signature AS photo, signature_type AS photo_type, user_id::text, branch_code, form, status
                           FROM {T} WHERE id::text = %s AND signature IS NOT NULL""", (visit_id,))


def photo(photo_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT p.photo, p.photo_type, v.user_id::text, v.branch_code, v.form, v.status
                           FROM {TP} p JOIN {T} v ON v.id = p.visit_id WHERE p.id::text = %s""", (photo_id,))


def attendance(form: str, date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    """Per PIC (an active account with the form page and a branch in scope): the days with a visit and the days off (no
    visit at all that day) from the start of the period to today."""
    from app import access
    from app.utils import today as wib_today
    end = min(date_to, wib_today())
    days = [] if end < date_from else [date_from + timedelta(days=i) for i in range((end - date_from).days + 1)]
    users = db.fetch("""SELECT u.id, u.username, u.full_name, u.role,
                                (SELECT array_agg(b.branch_code) FROM integration_portal.user_branch b WHERE b.user_id = u.id) AS codes
                         FROM integration_portal.user_account u WHERE u.is_active AND u.role <> 'superadmin'""")
    scope = None if branches is None else set(branches)
    pics = [u for u in users if u["codes"] and (scope is None or scope & set(u["codes"])) and f"form.{form}" in access.pages_for(u)]
    rows = db.fetch(f"""SELECT user_id::text AS u, (submitted_at AT TIME ZONE 'Asia/Jakarta')::date AS d, count(*)::int AS n FROM {T}
                         WHERE form = %s AND status = 'SUBMITTED' AND (submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s
                         GROUP BY 1, 2""", (form, date_from, end))
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        by.setdefault(r["u"], {})[r["d"].isoformat()] = r["n"]
    out = []
    for u in pics:
        mine = by.get(str(u["id"]), {})
        active = sum(1 for d in days if d.isoformat() in mine)
        out.append({"id": str(u["id"]), "username": u["username"], "name": u["full_name"] or u["username"], "branches": len(u["codes"]),
                    "active": active, "off": len(days) - active, "visits": sum(mine.values()), "days": mine})
    out.sort(key=lambda x: (-x["off"], x["name"].lower()))
    return {"days": [d.isoformat() for d in days], "pics": out}
