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
DAILY_SINCE = date(2026, 10, 23)  # one visit per branch per day from here; days off are counted from this day


class VisitError(CheckInError):
    """A visit refused with a message for the PIC (HTTP 422)."""


REQ, OPT = "required", "optional"


def _it(prefix: str, no: int, label: str, label_en: str, *, kind: str = "rating", required: bool = True,
        required_for: Optional[list[str]] = None, note: Optional[str] = None, photo: Optional[str] = None, **extra) -> dict:
    """One item. required: it must be rated before sending (required_for: only at the outlets of these branch groups,
    Monitoring → Branch groups). note / photo: what a rated required item needs (both by default); an optional item
    needs neither."""
    item = {"key": f"{prefix}.{no}", "no": no, "label": label, "labelEn": label_en, "kind": kind, "required": required,
            "note": note or REQ, "photo": photo or REQ, **extra}
    if required_for:
        item["requiredFor"] = required_for
    return item


def _list(prefix: str, rows: list[tuple]) -> list[dict]:
    """[(label, label_en, options?), ...] numbered from 1."""
    return [_it(prefix, i, r[0], r[1], **(r[2] if len(r) > 2 else {})) for i, r in enumerate(rows, start=1)]


def _sec(key: str, title: str, title_en: str, items: list[dict]) -> dict:
    return {"key": key, "title": title, "titleEn": title_en, "items": items}


def _grp(key: str, title: str, title_en: str, subtitle: str, subtitle_en: str, sections: list[dict]) -> dict:
    return {"key": key, "title": title, "titleEn": title_en, "subtitle": subtitle, "subtitleEn": subtitle_en, "sections": sections}


O = {"required": False}  # optional item

# Stock preparation and shelf-life labels (2026-10-27, Operations): the premixes and liquids below are required at every
# outlet (CTG, CTGP, CCI); the rest optional.
PREMIX = [
    ("Premix Premium", "Premix Premium", {}), ("Premix Regular", "Premix Regular", {}),
    ("Premix Premium Oat", "Premix Premium Oat", {}), ("Premix Smooth", "Premix Smooth", {}),
    ("Premix Magic", "Premix Magic", {}), ("Premix Jefferson", "Premix Jefferson", O),
    ("Premix Coklat", "Premix Chocolate", {}), ("Premix Tea Series", "Premix Tea Series", {}),
    ("Premix Matcha", "Premix Matcha", {}), ("Premix Taro", "Premix Taro", O),
    ("Premix Dirty & Ice White", "Premix Dirty & Ice White", O), ("Cold Brew Liquid", "Cold Brew Liquid", {}),
    ("Cream Base", "Cream Base", {}), ("Creamer Liquid", "Creamer Liquid", {}),
    ("Dimsum", "Dim Sum", O), ("Roti Kukus", "Steamed Bun", O), ("Puding", "Pudding", O),
    ("Side Dish Lainnya", "Other Side Dishes", O), ("Maincourse", "Main Course", O),
]
EVIDENCE_NOTE_ONLY = {"note": REQ, "photo": OPT}  # a note is required, the photo is not

DEFINITIONS: dict[str, dict] = {
    "product": {
        "title": "Monitoring Product",
        "titleEn": "Monitoring Product",
        "groups": [
            _grp("availability", "Availability", "Availability", "Ketersediaan dan Kesesuaian Produk", "Product availability and conformity", [
                _sec("stok-preparation", "Stock Preparation", "Stock Preparation",
                     _list("stok-preparation", [(f"Ketersediaan Stok {i}", f"{e} stock available", o) for i, e, o in PREMIX])),
                _sec("shelf-life", "Shelf Life Label", "Shelf Life Label",
                     _list("shelf-life", [(f"Shelf Life Label {i}", f"{e} shelf-life label", o) for i, e, o in PREMIX])),
                # Beans Specialty: required at the CTGP and CCI outlets only (2026-10-27)
                _sec("espresso-beans", "Espresso dan Beans", "Espresso and Beans", _list("espresso-beans", [
                    ("Ketersediaan Stok Espresso Premium", "Espresso Premium stock available"),
                    ("Ketersediaan Stok Espresso Regular", "Espresso Regular stock available"),
                    ("Ketersediaan Stok Beans Premium", "Beans Premium stock available", O),
                    ("Ketersediaan Stok Beans Regular", "Beans Regular stock available", O),
                    ("Ketersediaan Stok Beans Cold Brew", "Cold Brew beans stock available"),
                    ("Ketersediaan Stok Beans Specialty", "Specialty beans stock available", {"required": False, "required_for": ["CTGP", "CCI"]}),
                ])),
                _sec("bahan-penolong", "Bahan Penolong", "Supporting Materials", _list("bahan-penolong", [
                    ("Ketersediaan Cup Es", "Iced cups available"), ("Ketersediaan Cup Frosted", "Frosted cups available"),
                    ("Ketersediaan Cup Jefferson", "Jefferson cups available", O), ("Ketersediaan Paper Cup", "Paper cups available"),
                    ("Ketersediaan Es Batu", "Ice cubes available"), ("Ketersediaan Es Batu dalam Klip", "Ice cubes in clip bags available"),
                    ("Ketersediaan Can Dan Tutup Can (Sudah Stiker)", "Cans and can lids available (stickered)"),
                    ("Ketersediaan Botol Dan Tutup Botol (Sudah Stiker)", "Bottles and bottle caps available (stickered)"),
                    ("Ketersediaan Straw 5mm", "5 mm straws available"), ("Ketersediaan Straw 6mm", "6 mm straws available", O),
                    ("Ketersediaan Lunch Box S", "Lunch boxes S available", O), ("Ketersediaan Lunch Box M", "Lunch boxes M available", O),
                    ("Ketersediaan Set Spork", "Spork sets available", O), ("Ketersediaan Greeting Card", "Greeting cards available"),
                    ("Ketersediaan Beans Card Specialty", "Specialty beans cards available", O),
                    ("Ketersediaan Plastik Take Away", "Take-away bags available"),
                ])),
                _sec("bahan-baku", "Bahan Baku", "Raw Materials", _list("bahan-baku", [
                    ("Kesesuaian Form Request Bahan Baku", "Raw material request form matches"),
                    ("Kesesuaian Form Request FM, Oat Milk, Ice Cream, Brookfarm", "Request form FM, Oat Milk, Ice Cream, Brookfarm matches"),
                    ("Kesesuaian Form Request Ice Hot, Rich Gold", "Request form Ice Hot, Rich Gold matches", O),
                    ("Kesesuaian Form Request Bahan Baku Maincourse", "Main course raw material request form matches", O),
                    ("Kesesuaian Form Request Bahan Baku Side Dishes", "Side dish raw material request form matches"),
                ])),
            ]),
            _grp("kualitas-produk", "Kualitas Produk", "Product Quality", "5 Indra - Rasa", "5 Senses - Taste", [
                _sec("kualitas-produk", "Kualitas Produk", "Product Quality", _list("kualitas-produk", [
                    ("Cupping Premium", "Cupping Premium"), ("Cupping Regular", "Cupping Regular"),
                    ("Cupping All Premix", "Cupping all premixes"), ("Cupping All Cream & Foam", "Cupping all cream & foam"),
                    ("Cupping Menu Specialty", "Cupping specialty menu"),
                    ("Test Food WIP Side Dish & Main Course", "Test food WIP side dish & main course", O),
                    ("Test Food Menu Side Dish", "Test food side dish menu", O),
                    ("Test Food Menu Main Course", "Test food main course menu", O),
                    ("Handling & Treatment Product", "Product handling & treatment"),
                ])),
            ]),
        ],
    },
    "outlet": {
        "title": "Monitoring Outlet",
        "titleEn": "Monitoring Outlet",
        "groups": [
            _grp("kebersihan-rating", "Kebersihan dan Rating Outlet", "Outlet Cleanliness and Rating", "5 Indra - Penglihatan", "5 Senses - Sight", [
                _sec("kebersihan", "Kebersihan Outlet", "Outlet Cleanliness", _list("kebersihan", [
                    ("Kebersihan Area Bar Bersih", "Clean bar area"), ("Kebersihan Area Bar Kotor", "Dirty bar area"),
                    ("Kebersihan Area Kitchen", "Kitchen area"), ("Kebersihan Tools", "Tools"),
                    ("Kebersihan Area Facade / Halaman Depan", "Facade / front yard"), ("Kebersihan Area Indoor", "Indoor area"),
                    ("Kebersihan Area Outdoor", "Outdoor area"), ("Kebersihan Area Toilet & Musholla", "Toilet & prayer room"),
                    ("Kebersihan Area Warehouse", "Warehouse area"), ("Kebersihan Area Mess", "Staff mess"),
                ])),
                # platform = the stars of the outlet on that platform go with the item (2026-10-21): Google from
                # Rating & Review → Google Maps (automatic), the delivery apps typed by the PIC
                _sec("rating", "Rating Outlet", "Outlet Rating", _list("rating", [
                    ("Rating Google", "Google rating", {"platform": "google"}),
                    ("Rating GoFood", "GoFood rating", {"platform": "gofood"}),
                    ("Rating GrabFood", "GrabFood rating", {"platform": "grabfood"}),
                    ("Rating ShopeeFood", "ShopeeFood rating", {"platform": "shopeefood"}),
                    ("Complain Customer (7 hari ke belakang)", "Customer complaints (last 7 days)"),
                ])),
            ]),
            _grp("barista-flow", "Barista + Flow Bar", "Barista + Bar Flow", "Memastikan peran dan tanggung jawab di bar",
                 "Roles and responsibilities in the bar", [
                _sec("barista", "Barista", "Barista", _list("barista", [
                    ("Grooming Barista", "Barista grooming"),
                    ("Kedisiplinan Barista", "Barista discipline", O),
                    ("Nama Barista Telat", "Names of late baristas", {"kind": "text", "required": False}),
                    ("Update Rekap Absen Barista", "Barista attendance recap updated"),
                ])),
                # Speed of service and roles: a note is required, no photo needed; the bar flow: note and photo
                _sec("flow-bar", "Flow Bar", "Bar Flow", _list("flow-bar", [
                    ("Speed Of Service", "Speed of service", EVIDENCE_NOTE_ONLY),
                    ("Kesesuaian Role Barista", "Barista roles followed", EVIDENCE_NOTE_ONLY),
                    ("Kesesuaian Flow Bar", "Bar flow followed"),
                ])),
            ]),
            _grp("hospitality", "Hospitality", "Hospitality", "5 Indra - Pendengaran - Product Knowledge", "5 Senses - Hearing - Product knowledge", [
                _sec("hospitality", "Hospitality", "Hospitality", _list("hospitality", [
                    ("Product Knowledge Barista", "Barista product knowledge", EVIDENCE_NOTE_ONLY),
                    ("Greetings Barista", "Barista greetings", EVIDENCE_NOTE_ONLY),
                    ("Offering Product Barista", "Barista product offering", EVIDENCE_NOTE_ONLY),
                    ("Music", "Music", EVIDENCE_NOTE_ONLY),
                ])),
            ]),
            _grp("smell", "Aroma", "Smell", "5 Indra - Wangi Outlet - Alat Kebersihan", "5 Senses - Outlet scent - Cleaning tools", [
                _sec("alat-kebersihan", "Kelengkapan Alat Kebersihan", "Cleaning Tools Complete", _list("alat-kebersihan", [
                    ("Sapu", "Broom"), ("Pengki", "Dustpan"), ("Pel Area Bar", "Bar area mop"), ("Pel Area Customer", "Customer area mop"),
                    ("Wipper", "Squeegee"), ("Pel Karet", "Rubber mop"), ("Sikat Mesin Kopi & Grinder", "Coffee machine & grinder brush", O),
                    ("Lap Area Bar Depan", "Front bar cloth"), ("Lap Area Kitchen & Bar Belakang", "Kitchen & back bar cloth"),
                    ("Lap Area Customer", "Customer area cloth"), ("Lap Gelas", "Glass cloth", O), ("Lap Tools", "Tools cloth"),
                    ("Lap Mesin Kopi", "Coffee machine cloth", O), ("Lap Steam Milk", "Steam wand cloth", O),
                    ("Lap Basket Porta", "Portafilter basket cloth", O),
                ])),
                _sec("chemical", "Kelengkapan Chemical", "Chemicals Complete", _list("chemical", [
                    ("Sabun Kaca", "Glass cleaner"), ("Sabun Cuci Piring", "Dish soap"), ("Sabun Cuci Tangan", "Hand soap"),
                    ("Sabun Lantai", "Floor cleaner"), ("Sabun Toilet", "Toilet cleaner", O), ("Pengharum AC", "AC freshener"),
                    ("Pengharum Ruangan", "Room freshener"), ("Pengharum Mushola & Toilet", "Prayer room & toilet freshener", O),
                ])),
            ]),
            _grp("kenyamanan", "Kenyamanan Outlet", "Outlet Comfort", "5 Indra - Suhu Ruangan & Kenyamanan Outlet",
                 "5 Senses - Room temperature & outlet comfort", [
                _sec("kenyamanan", "Kenyamanan Outlet", "Outlet Comfort", _list("kenyamanan", [
                    ("Kondisi Ruangan", "Room condition"),
                    ("Kondisi Furniture (Sofa, Meja, Karpet, Kursi)", "Furniture condition (sofa, table, carpet, chair)"),
                    ("Kondisi Internet Customer", "Customer internet"), ("Kondisi Merchandise", "Merchandise condition", O),
                ])),
            ]),
            # added 2026-10-21 (user request): the workbook's ADMINISTRASI & FINANCE; SO Packaging became SO Daily (2026-10-27)
            _grp("administrasi-finance", "Administrasi & Finance", "Administration & Finance", "Administrasi & Finance Outlet",
                 "Outlet administration & finance", [
                _sec("administrasi-finance", "Administrasi & Finance", "Administration & Finance", _list("administrasi-finance", [
                    ("SO Daily", "Daily stock opname"), ("SO Weekly / Monthly", "Weekly / monthly stock opname"),
                    ("Daily Cupping", "Daily cupping"), ("Dokumentasi Surat Jalan", "Delivery note documentation"),
                    ("Form Selfpickup", "Self-pickup form", O), ("Penggunaan Pitty Cash Terkontrol", "Petty cash use under control"),
                    ("Setoran Uang Pembelian Menggunakan Cash", "Cash purchase money deposited"),
                ])),
            ]),
        ],
    },
}


def all_items(form: str) -> list[dict]:
    """Every item of a form with its group and section."""
    out = []
    for g in DEFINITIONS[form]["groups"]:
        for s in g["sections"]:
            for it in s["items"]:
                out.append({**it, "group": g["key"], "section": s["key"], "section_title": s["title"],
                            "section_title_en": s.get("titleEn", s["title"])})
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


def group_of(branch_code: str) -> Optional[str]:
    """The branch group code (CCI, CTG, CTGP …, Monitoring → Branch groups) of an outlet, None when it has none."""
    r = db.fetchrow("""SELECT g.code FROM integration_portal.branch_group_member m JOIN integration_portal.branch_group g ON g.id = m.group_id
                       WHERE m.branch_code = %s""", (branch_code,))
    return r["code"] if r else None


def is_required(item: dict, group: Optional[str]) -> bool:
    """Must the item be rated before sending, at an outlet of this branch group?"""
    return bool(item.get("required", True)) or (group is not None and group in (item.get("requiredFor") or []))


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
    out["branchGroup"] = group_of(row["branch_code"])  # which items are required at this outlet
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


def missing(form: str, answers: dict, photos: list[dict], group: Optional[str] = None) -> list[dict]:
    """What blocks the submit (2026-10-27): a required item not rated (need "rating"), or a rated required item without
    the note / photo it asks for (need "note", "photo" or "note+photo"). Optional items block nothing; the platform stars
    of Rating Outlet need neither a note nor a photo. `group` = the outlet's branch group (an item may be required only
    at CTGP / CCI outlets)."""
    with_photo = {p["item"] for p in photos}
    out = []
    for it in all_items(form):
        if it["kind"] != "rating" or not is_required(it, group):
            continue
        a = answers.get(it["key"]) or {}
        if not a.get("rating"):
            out.append({"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "need": "rating"})
            continue
        if it.get("platform"):
            continue
        need = [x for x, ok in (("note", it.get("note") != "required" or bool(a.get("note"))),
                                ("photo", it.get("photo") != "required" or it["key"] in with_photo)) if not ok]
        if need:
            out.append({"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "need": "+".join(need)})
    return out


def submit(form: str, user: dict, visit_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float],
           device: Optional[str], ip: Optional[str], signature: Optional[str] = None, signed_name: Optional[str] = None) -> dict:
    row = own_draft(form, user, visit_id)
    _not_today(form, user, row["branch_code"], row["branch_name"])
    photos = photos_of(visit_id)
    todo = missing(form, row["answers"] or {}, photos, group_of(row["branch_code"]))
    if any(t["need"] == "rating" for t in todo):
        raise VisitError(f"Belum bisa dikirim: {sum(1 for t in todo if t['need'] == 'rating')} butir wajib belum dinilai")
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
            s = sections.setdefault(it["section"], {"key": it["section"], "title": it["section_title"], "titleEn": it["section_title_en"],
                                                    "group": it["group"], "under": 0, "meet": 0, "exceed": 0})
            s[rating.lower()] += 1
            if rating == "UNDER":
                f = findings.setdefault(it["key"], {"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"),
                                                    "section": it["section_title"], "sectionEn": it["section_title_en"],
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
            platforms.append({"key": it["key"], "platform": it["platform"], "label": it["label"], "labelEn": it.get("labelEn"),
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
    visit at all that day), counted from DAILY_SINCE (the daily rule) and up to yesterday — today is no day off yet."""
    from app import access
    from app.utils import today as wib_today
    now = wib_today()
    start, end = max(date_from, DAILY_SINCE), min(date_to, now)
    days = [] if end < start else [start + timedelta(days=i) for i in range((end - start).days + 1)]
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
        off = sum(1 for d in days if d < now and d.isoformat() not in mine)
        out.append({"id": str(u["id"]), "username": u["username"], "name": u["full_name"] or u["username"], "branches": len(u["codes"]),
                    "active": active, "off": off, "visits": sum(mine.values()), "days": mine})
    out.sort(key=lambda x: (-x["off"], x["name"].lower()))
    return {"days": [d.isoformat() for d in days], "today": now.isoformat(), "since": DAILY_SINCE.isoformat(), "pics": out}
