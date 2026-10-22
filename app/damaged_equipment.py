"""Form → Damaged Equipment and Monitoring → Damaged Equipment (2026-10-21, migration 027).

A PIC requests, for one of their branches, either a REPAIR (Maintenance: building & furniture, heavy tools …) or a
PROCUREMENT (decoration, tools & heavy tools to add). A request is a draft until it is sent: items (category from the
master of 6 groups, name, qty, location, urgency), an analysis, up to MAX_PHOTOS camera photos and one video of at most
MAX_VIDEO_BYTES, a preview and the PIC's signature (required). Sending gives it a number (DE-YYMM-NNNN), a PDF and a
share link (read-only, no login, SHARE days).

The team moves it through FLOW: PENDING → VALIDATION → REVIEW → ACCEPTED → IN_PROGRESS → DONE, with ON_HOLD (resumes
where it stopped) and REJECTED; a note is required to hold, reject, swap or mark a duplicate; IN_PROGRESS needs a
technician, DONE a photo of the work done (AFTER). Technicians are assigned (several per request) and can be moved to
a request that needs them first (a swap: released here with the reason, assigned there). Every step is an event: the
timeline the PIC follows and the time tracker (time per stage, on hold apart, SLA per urgency).

To avoid double requests the form shows the open requests of the branch, and an item already open there is flagged;
the team can close a request as a duplicate of another one.
"""
import hashlib
import json

import secrets
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db
from app.config import get_settings
from app.pic_checkin import CheckInError, branch_options, judge, locations, parse_photo

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
MAX_PHOTOS = 10
MAX_AFTER = 10
MAX_INVOICES = 10
MAX_INVOICE_BYTES = 10 * 1024 * 1024
WAGE_TYPES = ("DAILY", "MONTHLY")
MAX_VIDEO_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 50
KINDS = {"REPAIR": "Perbaikan (Building & Tools)", "PROCUREMENT": "Pengadaan (Dekorasi & Tools)"}
URGENCY = ("CRITICAL", "HIGH", "NORMAL", "LOW", "PLANNED")  # most urgent first
URGENCY_LABEL = {"CRITICAL": "Kritis", "HIGH": "Tinggi", "NORMAL": "Normal", "LOW": "Rendah", "PLANNED": "Dijadwalkan"}
# hours from sending to done (on hold not counted); PLANNED has no target
SLA_HOURS = {"CRITICAL": 24, "HIGH": 72, "NORMAL": 168, "LOW": 336}
CAUSES = ("WEAR", "MISUSE", "AGE", "OTHER")
STATUS_LABEL = {"DRAFT": "Draf", "PENDING": "Pending", "VALIDATION": "Validasi", "REVIEW": "Review", "ON_HOLD": "On hold",
                "ACCEPTED": "Accept", "IN_PROGRESS": "Dikerjakan", "DONE": "Done", "REJECTED": "Reject"}
OPEN = ("PENDING", "VALIDATION", "REVIEW", "ON_HOLD", "ACCEPTED", "IN_PROGRESS")
FLOW = {
    "PENDING": ("VALIDATION", "ON_HOLD", "REJECTED"),
    "VALIDATION": ("REVIEW", "ON_HOLD", "REJECTED"),
    "REVIEW": ("ACCEPTED", "ON_HOLD", "REJECTED"),
    "ACCEPTED": ("IN_PROGRESS", "ON_HOLD", "REJECTED"),
    "IN_PROGRESS": ("DONE", "ON_HOLD"),
    "ON_HOLD": ("REJECTED",),  # + resume to hold_from
}
NOTE_REQUIRED = ("ON_HOLD", "REJECTED")
VIDEO_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "3gp": "video/3gpp"}


class DeError(CheckInError):
    """A request refused with a message for the user (HTTP 422)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


# ---------------------------------------------------------------- master: categories & technicians

def categories(active_only: bool = True) -> list[dict]:
    rows = db.fetch(f"""SELECT id, grp, name, default_urgency, examples, sort, is_active FROM {P}.de_category
                        {"WHERE is_active" if active_only else ""} ORDER BY sort, name""")
    return [{"id": r["id"], "group": r["grp"], "name": r["name"], "defaultUrgency": r["default_urgency"],
             "examples": list(r["examples"] or []), "sort": r["sort"], "isActive": r["is_active"]} for r in rows]


def save_category(category_id: Optional[int], body: dict) -> dict:
    if body.get("defaultUrgency") not in URGENCY:
        raise DeError("Urgensi tidak dikenal")
    examples = [e.strip()[:80] for e in body.get("examples") or [] if e and e.strip()][:30]
    params = (body["group"].strip()[:60], body["name"].strip()[:80], body["defaultUrgency"], examples,
              int(body.get("sort") or 0), bool(body.get("isActive", True)))
    try:
        if category_id is None:
            row = db.fetchrow(f"""INSERT INTO {P}.de_category (grp, name, default_urgency, examples, sort, is_active)
                                  VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""", params)
        else:
            row = db.fetchrow(f"""UPDATE {P}.de_category SET grp = %s, name = %s, default_urgency = %s, examples = %s, sort = %s,
                                  is_active = %s, updated_at = now() WHERE id = %s RETURNING id""", (*params, category_id))
    except Exception as e:  # noqa: BLE001 - unique name
        if "de_category_name_key" in str(e):
            raise DeError("Nama kategori sudah dipakai") from None
        raise
    if row is None:
        raise LookupError("Kategori tidak ditemukan")
    return next(c for c in categories(False) if c["id"] == row["id"])


def technicians(active_only: bool = False) -> list[dict]:
    rows = db.fetch(f"""SELECT t.*, (SELECT count(*)::int FROM {P}.de_assignment a WHERE a.technician_id = t.id AND a.status = 'ACTIVE') AS active,
                               (SELECT count(*)::int FROM {P}.de_assignment a WHERE a.technician_id = t.id AND a.status = 'DONE') AS done
                        FROM {P}.de_technician t {"WHERE t.is_active" if active_only else ""} ORDER BY t.is_active DESC, t.name""")
    jobs = db.fetch(f"""SELECT a.technician_id, a.scheduled_for, r.id::text AS request_id, r.number, r.branch_name, r.urgency, r.status
                        FROM {P}.de_assignment a JOIN {P}.de_request r ON r.id = a.request_id WHERE a.status = 'ACTIVE'
                        ORDER BY a.scheduled_for NULLS LAST, a.assigned_at""")
    by = {}
    for j in jobs:
        by.setdefault(j["technician_id"], []).append({"requestId": j["request_id"], "number": j["number"], "branchName": j["branch_name"],
                                                      "urgency": j["urgency"], "status": j["status"], "scheduledFor": _iso(j["scheduled_for"])})
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "company": r["company"], "skills": list(r["skills"] or []),
             "city": r["city"], "phone": r["phone"], "notes": r["notes"], "isActive": r["is_active"],
             "wageType": r["wage_type"], "wageAmount": _num(r["wage_amount"]),
             "activeJobs": r["active"], "doneJobs": r["done"], "jobs": by.get(r["id"], [])} for r in rows]


def save_technician(technician_id: Optional[int], body: dict) -> dict:
    if body.get("kind") not in ("INTERNAL", "VENDOR"):
        raise DeError("Jenis tukang harus internal atau vendor")
    name = " ".join((body.get("name") or "").split())[:120]
    if not name:
        raise DeError("Nama tukang wajib diisi")
    wage_type = body.get("wageType") or "DAILY"
    if wage_type not in WAGE_TYPES:
        raise DeError("Jenis gaji harus harian atau bulanan")
    wage = body.get("wageAmount")
    if wage is not None and (float(wage) < 0 or float(wage) > 1e12):
        raise DeError("Nominal gaji tidak valid")
    params = (name, body["kind"], (body.get("company") or "").strip()[:120] or None,
              [s.strip()[:60] for s in body.get("skills") or [] if s and s.strip()][:20], (body.get("city") or "").strip()[:80] or None,
              (body.get("phone") or "").strip()[:40] or None, (body.get("notes") or "").strip()[:500] or None, bool(body.get("isActive", True)),
              wage_type, wage)
    if technician_id is None:
        row = db.fetchrow(f"""INSERT INTO {P}.de_technician (name, kind, company, skills, city, phone, notes, is_active, wage_type, wage_amount)
                              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", params)
    else:
        row = db.fetchrow(f"""UPDATE {P}.de_technician SET name = %s, kind = %s, company = %s, skills = %s, city = %s, phone = %s,
                              notes = %s, is_active = %s, wage_type = %s, wage_amount = %s, updated_at = now() WHERE id = %s RETURNING id""",
                          (*params, technician_id))
    if row is None:
        raise LookupError("Tukang tidak ditemukan")
    return next(t for t in technicians() if t["id"] == row["id"])


# ---------------------------------------------------------------- media files (backend volume)

def media_root() -> Path:
    root = Path(get_settings().media_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store(request_id: str, kind: str, data: bytes, mime: str, ext: str, user: dict, file_name: Optional[str] = None,
           taken_at: Optional[datetime] = None, lat: Optional[float] = None, lng: Optional[float] = None, note: Optional[str] = None) -> None:
    media_id = str(uuid.uuid4())
    rel = f"de/{_now():%Y/%m}/{media_id}.{ext}"
    path = media_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.fetchrow(f"""INSERT INTO {P}.de_media (id, request_id, kind, storage_path, mime, bytes, sha256, file_name, taken_at, latitude,
                    longitude, uploaded_by, uploaded_by_name, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (media_id, request_id, kind, rel, mime, len(data), hashlib.sha256(data).hexdigest(), file_name, taken_at, lat, lng,
                 user["id"], user.get("full_name") or user["username"], note))


def media_of(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id::text, kind, mime, bytes, file_name, taken_at, uploaded_at, uploaded_by_name, latitude, longitude, note
                        FROM {P}.de_media WHERE request_id::text = %s ORDER BY uploaded_at""", (request_id,))
    return [{"id": r["id"], "kind": r["kind"], "mime": r["mime"], "bytes": r["bytes"], "fileName": r["file_name"],
             "takenAt": _iso(r["taken_at"]), "uploadedAt": _iso(r["uploaded_at"]), "uploadedBy": r["uploaded_by_name"], "note": r["note"]} for r in rows]


def _invoice_type(data: bytes) -> tuple[str, str]:
    """Invoices / receipts: a PDF or a photo (scan); anything else is refused."""
    if data[:5] == b"%PDF-":
        return "application/pdf", "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise DeError("Invoice harus PDF atau foto (JPG, PNG, WebP)")


def add_invoice(user: dict, request_id: str, data: bytes, file_name: str, note: Optional[str]) -> dict:
    """An invoice / receipt of the cost, attached by the team while the request is handled (≤ MAX_INVOICES)."""
    row = get_row(request_id)
    if row is None or row["status"] in ("DRAFT", "REJECTED"):
        raise DeError("Invoice dilampirkan pada pengajuan yang sedang ditangani")
    if not data:
        raise DeError("File kosong")
    if len(data) > MAX_INVOICE_BYTES:
        raise DeError(f"File invoice maksimal {MAX_INVOICE_BYTES // 1048576} MB")
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id::text = %s AND kind = 'INVOICE'", (request_id,))["n"]
    if n >= MAX_INVOICES:
        raise DeError(f"Maksimal {MAX_INVOICES} invoice; hapus satu dulu")
    mime, ext = _invoice_type(data)
    _store(request_id, "INVOICE", data, mime, ext, user, file_name=(file_name or "")[:160] or None, note=(note or "").strip()[:200] or None)
    with db.transaction() as conn:
        _event(conn, request_id, user, "COST", None, None, (note or "").strip()[:200] or None, {"invoice": (file_name or "")[:160]})
    return {"media": media_of(request_id)}


def media_file(media_id: str) -> Optional[dict]:
    row = db.fetchrow(f"""SELECT m.storage_path, m.mime, m.file_name, m.kind, m.request_id::text, r.user_id::text, r.branch_code, r.status
                          FROM {P}.de_media m JOIN {P}.de_request r ON r.id = m.request_id WHERE m.id::text = %s""", (media_id,))
    if row:
        path = media_root() / row["storage_path"]
        row["path"] = path if path.is_file() else None
    return row


def _video_type(data: bytes, file_name: str) -> tuple[str, str]:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime", "mov"
        if brand.startswith(b"3g"):
            return "video/3gpp", "3gp"
        return "video/mp4", "mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "webm"
    raise DeError(f"File {ext or 'ini'} bukan video yang didukung (MP4, MOV, WebM, 3GP)")


# ---------------------------------------------------------------- requests: draft (PIC)

COLS = """r.id::text, r.number, r.kind, r.status, r.hold_from, r.user_id::text, r.username, r.full_name, r.branch_code, r.branch_name,
    r.analysis, r.cause, r.urgency, r.latitude, r.longitude, r.accuracy_m, r.distance_m, r.outside_radius, r.signed_name, r.signed_at,
    (r.signature IS NOT NULL) AS has_signature, r.share_token, r.share_expires_at, r.est_cost, r.actual_cost, r.vendor,
    r.duplicate_of::text, r.created_at, r.updated_at, r.submitted_at, r.closed_at,
    (SELECT number FROM integration_portal.de_request d WHERE d.id = r.duplicate_of) AS duplicate_number"""


def get_row(request_id: str) -> Optional[dict]:
    try:
        uuid.UUID(request_id)
    except ValueError:
        return None
    return db.fetchrow(f"SELECT {COLS} FROM {P}.de_request r WHERE r.id::text = %s", (request_id,))


def items_of(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT i.id, i.category_id, c.grp, c.name AS category, i.name, i.qty, i.location, i.urgency, i.note
                        FROM {P}.de_item i JOIN {P}.de_category c ON c.id = i.category_id WHERE i.request_id::text = %s
                        ORDER BY i.sort, i.id""", (request_id,))
    return [{"id": r["id"], "categoryId": r["category_id"], "group": r["grp"], "category": r["category"], "name": r["name"],
             "qty": r["qty"], "location": r["location"], "urgency": r["urgency"], "note": r["note"]} for r in rows]


def events_of(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id, at, actor_name, kind, from_status, to_status, note, details FROM {P}.de_event
                        WHERE request_id::text = %s ORDER BY at, id""", (request_id,))
    return [{"id": r["id"], "at": _iso(r["at"]), "actor": r["actor_name"], "kind": r["kind"], "from": r["from_status"],
             "to": r["to_status"], "note": r["note"], "details": r["details"] or {}} for r in rows]


def assignments_of(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT a.id, a.technician_id, t.name, t.kind, t.company, t.phone, a.scheduled_for, a.status, a.note, a.assigned_at,
                               a.assigned_by, a.released_at, a.released_note
                        FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                        WHERE a.request_id::text = %s ORDER BY a.status = 'ACTIVE' DESC, a.assigned_at""", (request_id,))
    return [{"id": r["id"], "technicianId": r["technician_id"], "name": r["name"], "kind": r["kind"], "company": r["company"],
             "phone": r["phone"], "scheduledFor": _iso(r["scheduled_for"]), "status": r["status"], "note": r["note"],
             "assignedAt": _iso(r["assigned_at"]), "assignedBy": r["assigned_by"], "releasedAt": _iso(r["released_at"]),
             "releasedNote": r["released_note"]} for r in rows]


def tracker(row: dict, events: list[dict]) -> dict:
    """Time per status from sending to now / closing; on hold kept apart; SLA by urgency (on hold not counted)."""
    if not row["submitted_at"]:
        return {"stages": [], "totalHours": None, "holdHours": 0, "workHours": None, "slaHours": SLA_HOURS.get(row["urgency"]),
                "overdue": False, "progress": 0}
    marks = [(datetime.fromisoformat(e["at"]), e["to"]) for e in events if e["kind"] in ("SUBMIT", "STATUS") and e["to"]]
    end = row["closed_at"] or _now()
    stages: dict[str, float] = {}
    for (start, st), nxt in zip(marks, [m[0] for m in marks[1:]] + [end]):
        stages[st] = stages.get(st, 0.0) + max(0.0, (nxt - start).total_seconds() / 3600)
    total = max(0.0, (end - row["submitted_at"]).total_seconds() / 3600)
    hold = stages.get("ON_HOLD", 0.0)
    for st in ("DONE", "REJECTED"):
        stages.pop(st, None)
    sla = SLA_HOURS.get(row["urgency"])
    order = ["PENDING", "VALIDATION", "REVIEW", "ACCEPTED", "IN_PROGRESS", "DONE"]
    current = row["hold_from"] if row["status"] == "ON_HOLD" else row["status"]
    progress = 100 if row["status"] == "DONE" else (round(order.index(current) * 100 / (len(order) - 1)) if current in order else 0)
    return {"stages": [{"status": s, "label": STATUS_LABEL[s], "hours": round(h, 2)} for s, h in stages.items()],
            "totalHours": round(total, 2), "holdHours": round(hold, 2), "workHours": round(total - hold, 2), "slaHours": sla,
            "overdue": bool(sla and row["status"] in OPEN and total - hold > sla), "progress": progress}


def view(row: dict, full: bool = False) -> dict:
    out = {
        "id": row["id"], "number": row["number"], "kind": row["kind"], "kindLabel": KINDS[row["kind"]], "status": row["status"],
        "statusLabel": STATUS_LABEL[row["status"]], "holdFrom": row["hold_from"], "userId": row["user_id"], "username": row["username"],
        "picName": row["full_name"] or row["username"], "branchCode": row["branch_code"], "branchName": row["branch_name"],
        "analysis": row["analysis"], "cause": row["cause"], "urgency": row["urgency"], "urgencyLabel": URGENCY_LABEL[row["urgency"]],
        "distanceM": row["distance_m"], "outsideRadius": row["outside_radius"],
        "signature": {"name": row["signed_name"], "signedAt": _iso(row["signed_at"])} if row["has_signature"] else None,
        "estCost": _num(row["est_cost"]), "actualCost": _num(row["actual_cost"]), "vendor": row["vendor"],
        "duplicateOf": {"id": row["duplicate_of"], "number": row["duplicate_number"]} if row["duplicate_of"] else None,
        "createdAt": _iso(row["created_at"]), "updatedAt": _iso(row["updated_at"]), "submittedAt": _iso(row["submitted_at"]),
        "closedAt": _iso(row["closed_at"]),
    }
    if full:
        events = events_of(row["id"])
        out.update(items=items_of(row["id"]), media=media_of(row["id"]), events=events, assignments=assignments_of(row["id"]),
                   tracker=tracker(row, events))
    return out


def open_in_branch(branch_code: str, exclude: Optional[str] = None) -> list[dict]:
    """Open requests of a branch with their items: shown on the form to avoid asking twice."""
    rows = db.fetch(f"""SELECT {COLS} FROM {P}.de_request r WHERE r.branch_code = %s AND r.status = ANY(%s)
                        AND (%s::text IS NULL OR r.id::text <> %s) ORDER BY r.submitted_at DESC""",
                    (branch_code, list(OPEN), exclude, exclude))
    return [{**view(r), "items": items_of(r["id"])} for r in rows]


def form_state(user: dict) -> dict:
    mine = db.fetch(f"SELECT {COLS} FROM {P}.de_request r WHERE r.user_id::text = %s ORDER BY COALESCE(r.submitted_at, r.created_at) DESC LIMIT 200",
                    (str(user["id"]),))
    out = []
    for r in mine:
        v = view(r)
        if r["status"] != "DRAFT":
            v["tracker"] = tracker(r, events_of(r["id"]))
            v["lastNote"] = next((e for e in reversed(events_of(r["id"])) if e["note"]), None)
        out.append(v)
    return {"user": {"id": str(user["id"]), "name": user.get("full_name") or user["username"]}, "branches": branch_options(user),
            "categories": categories(), "kinds": KINDS, "urgency": URGENCY_LABEL, "requests": out,
            "limits": {"photos": MAX_PHOTOS, "videoBytes": MAX_VIDEO_BYTES, "items": MAX_ITEMS}}


def own_draft(user: dict, request_id: str) -> dict:
    row = get_row(request_id)
    if row is None or row["user_id"] != str(user["id"]):
        raise LookupError("Pengajuan tidak ditemukan")
    if row["status"] != "DRAFT":
        raise DeError("Pengajuan ini sudah dikirim")
    return row


def create_draft(user: dict, kind: str, branch_code: str) -> dict:
    if kind not in KINDS:
        raise DeError("Jenis pengajuan tidak dikenal")
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise DeError("Cabang tidak tersedia untuk akun Anda")
    row = db.fetchrow(f"""INSERT INTO {P}.de_request (kind, user_id, username, full_name, branch_code, branch_name)
                          VALUES (%s, %s, %s, %s, %s, %s) RETURNING id::text""",
                      (kind, user["id"], user["username"], user.get("full_name"), branch["code"], branch["name"]))
    return full(row["id"])


def full(request_id: str) -> dict:
    row = get_row(request_id)
    out = view(row, full=True)
    out["openInBranch"] = open_in_branch(row["branch_code"], exclude=request_id) if row["status"] in ("DRAFT", *OPEN) else []
    return out


def save_draft(user: dict, request_id: str, body: dict) -> dict:
    """Branch, analysis, cause and the items of a draft (the items are replaced as a whole)."""
    row = own_draft(user, request_id)
    sets: dict[str, Any] = {}
    if "branchCode" in body and body["branchCode"] != row["branch_code"]:
        branch = next((b for b in branch_options(user) if b["code"] == body["branchCode"]), None)
        if branch is None:
            raise DeError("Cabang tidak tersedia untuk akun Anda")
        sets.update(branch_code=branch["code"], branch_name=branch["name"])
    if "analysis" in body:
        sets["analysis"] = (body["analysis"] or "").strip()[:4000] or None
    if "cause" in body:
        if body["cause"] not in (None, "", *CAUSES):
            raise DeError("Penyebab tidak dikenal")
        sets["cause"] = body["cause"] or None
    with db.transaction() as conn:
        if "items" in body:
            items = body["items"] or []
            if len(items) > MAX_ITEMS:
                raise DeError(f"Maksimal {MAX_ITEMS} item per pengajuan")
            known = {c["id"]: c for c in categories()}
            conn.execute(f"DELETE FROM {P}.de_item WHERE request_id::text = %s", (request_id,))
            for n, it in enumerate(items):
                cat = known.get(int(it.get("categoryId") or 0))
                if cat is None:
                    raise DeError("Kategori item tidak dikenal")
                name = " ".join((it.get("name") or "").split())[:120]
                if not name:
                    raise DeError("Nama item wajib diisi")
                urgency = it.get("urgency") or cat["defaultUrgency"]
                if urgency not in URGENCY:
                    raise DeError("Urgensi tidak dikenal")
                conn.execute(f"""INSERT INTO {P}.de_item (request_id, category_id, name, qty, location, urgency, note, sort)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                             (request_id, cat["id"], name, max(1, min(9999, int(it.get("qty") or 1))),
                              (it.get("location") or "").strip()[:120] or None, urgency, (it.get("note") or "").strip()[:1000] or None, n))
            levels = [it.get("urgency") or known[int(it["categoryId"])]["defaultUrgency"] for it in items]
            sets["urgency"] = min(levels, key=URGENCY.index) if levels else "NORMAL"
        if sets:
            cols = ", ".join(f"{k} = %s" for k in sets)
            conn.execute(f"UPDATE {P}.de_request SET {cols}, updated_at = now() WHERE id::text = %s", (*sets.values(), request_id))
    return full(request_id)


def add_photo(user: dict, request_id: str, photo: str, lat: Optional[float], lng: Optional[float], taken_at: Optional[datetime],
              kind: str = "PHOTO") -> dict:
    limit = MAX_AFTER if kind == "AFTER" else MAX_PHOTOS
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id::text = %s AND kind = %s", (request_id, kind))["n"]
    if n >= limit:
        raise DeError(f"Maksimal {limit} foto; hapus satu dulu")
    mime, data = parse_photo(photo)
    _store(request_id, kind, data, mime, {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg"), user,
           taken_at=taken_at, lat=lat, lng=lng)
    return {"media": media_of(request_id)}


def add_video(user: dict, request_id: str, data: bytes, file_name: str) -> dict:
    own_draft(user, request_id)
    if not data:
        raise DeError("File video kosong")
    if len(data) > MAX_VIDEO_BYTES:
        raise DeError(f"Video maksimal {MAX_VIDEO_BYTES // 1048576} MB; rekam lebih pendek atau kecilkan resolusinya")
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.de_media WHERE request_id::text = %s AND kind = 'VIDEO'", (request_id,)):
        raise DeError("Satu video per pengajuan; hapus video sebelumnya dulu")
    mime, ext = _video_type(data, file_name)
    _store(request_id, "VIDEO", data, mime, ext, user, file_name=(file_name or "")[:160] or None)
    return {"media": media_of(request_id)}


def delete_media(request_id: str, media_id: str, kinds: tuple[str, ...]) -> dict:
    row = db.fetchrow(f"DELETE FROM {P}.de_media WHERE id::text = %s AND request_id::text = %s AND kind = ANY(%s) RETURNING storage_path",
                      (media_id, request_id, list(kinds)))
    if row:
        (media_root() / row["storage_path"]).unlink(missing_ok=True)
    return {"media": media_of(request_id)}


def discard(user: dict, request_id: str) -> None:
    own_draft(user, request_id)
    for m in media_of(request_id):
        delete_media(request_id, m["id"], ("PHOTO", "VIDEO", "AFTER"))
    db.fetchrow(f"DELETE FROM {P}.de_request WHERE id::text = %s AND status = 'DRAFT' RETURNING id", (request_id,))


def missing(row: dict, items: list[dict], media: list[dict]) -> list[str]:
    out = []
    if not items:
        out.append("Tambahkan minimal satu item")
    if not (row["analysis"] or "").strip():
        out.append("Isi analisis kerusakan" if row["kind"] == "REPAIR" else "Isi alasan pengadaan")
    if not any(m["kind"] == "PHOTO" for m in media):
        out.append("Lampirkan minimal satu foto" if row["kind"] == "REPAIR" else "Lampirkan minimal satu foto area di outlet")
    return out


def _next_number(conn) -> str:
    month = datetime.now(WIB).strftime("%y%m")
    n = conn.execute(f"""INSERT INTO {P}.de_counter (month, n) VALUES (%s, 1)
                         ON CONFLICT (month) DO UPDATE SET n = de_counter.n + 1 RETURNING n""", (month,)).fetchone()["n"]
    return f"DE-{month}-{n:04d}"


def _event(conn, request_id: str, user: Optional[dict], kind: str, from_status: Optional[str] = None, to_status: Optional[str] = None,
           note: Optional[str] = None, details: Optional[dict] = None) -> None:
    conn.execute(f"""INSERT INTO {P}.de_event (request_id, actor_id, actor_name, kind, from_status, to_status, note, details)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                 (request_id, user["id"] if user else None, (user.get("full_name") or user["username"]) if user else None, kind,
                  from_status, to_status, note, json.dumps(details or {}, ensure_ascii=False)))


def submit(user: dict, request_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float],
           signature: Optional[str], signed_name: Optional[str]) -> dict:
    row = own_draft(user, request_id)
    items, media = items_of(request_id), media_of(request_id)
    todo = missing(row, items, media)
    if todo:
        raise DeError("Belum bisa dikirim: " + "; ".join(todo))
    if not signature:
        raise DeError("Tanda tangan PIC wajib sebelum mengirim pengajuan")
    sig_type, sig = parse_photo(signature)
    signed_name = " ".join((signed_name or "").split())[:120]
    if not signed_name:
        raise DeError("Tulis nama penanda tangan di bawah tanda tangan")
    outlet = locations([row["branch_code"]]).get(row["branch_code"]) or {}
    j = judge(lat, lng, accuracy, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    token = secrets.token_urlsafe(24)
    with db.transaction() as conn:
        number = _next_number(conn)
        conn.execute(f"""UPDATE {P}.de_request SET number = %s, status = 'PENDING', submitted_at = now(), updated_at = now(),
                         latitude = %s, longitude = %s, accuracy_m = %s, distance_m = %s, outside_radius = %s,
                         signature = %s, signature_type = %s, signed_name = %s, signed_at = now(),
                         share_token = %s, share_expires_at = now() + %s::interval WHERE id::text = %s""",
                     (number, lat, lng, accuracy, j["distance_m"], j["outside_radius"], sig, sig_type, signed_name, token,
                      f"{get_settings().share_link_days} days", request_id))
        _event(conn, request_id, user, "SUBMIT", None, "PENDING", None, {"items": len(items), "photos": sum(m["kind"] == "PHOTO" for m in media)})
    return full(request_id)


# ---------------------------------------------------------------- monitoring (team)

def monitor(date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    where = ["r.status <> 'DRAFT'", "((r.submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s OR r.status = ANY(%(open)s))"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "open": list(OPEN)}
    if branches is not None:
        where.append("r.branch_code = ANY(%(br)s)")
        params["br"] = branches
    rows = db.fetch(f"SELECT {COLS} FROM {P}.de_request r WHERE {' AND '.join(where)} ORDER BY r.submitted_at DESC LIMIT 2000", params)
    ids = [r["id"] for r in rows]
    items = db.fetch(f"""SELECT i.request_id::text, c.grp, c.name AS category, i.name, i.qty, i.urgency FROM {P}.de_item i
                         JOIN {P}.de_category c ON c.id = i.category_id WHERE i.request_id::text = ANY(%s) ORDER BY i.sort""", (ids,))
    techs = db.fetch(f"""SELECT a.request_id::text, t.name FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                         WHERE a.status = 'ACTIVE' AND a.request_id::text = ANY(%s)""", (ids,))
    events = db.fetch(f"""SELECT request_id::text, at, kind, to_status FROM {P}.de_event WHERE request_id::text = ANY(%s)
                          AND kind IN ('SUBMIT', 'STATUS') ORDER BY at, id""", (ids,))
    it_by, te_by, ev_by = {}, {}, {}
    for i in items:
        it_by.setdefault(i["request_id"], []).append({"group": i["grp"], "category": i["category"], "name": i["name"], "qty": i["qty"],
                                                      "urgency": i["urgency"]})
    for t in techs:
        te_by.setdefault(t["request_id"], []).append(t["name"])
    for e in events:
        ev_by.setdefault(e["request_id"], []).append({"at": _iso(e["at"]), "kind": e["kind"], "to": e["to_status"]})
    data = []
    for r in rows:
        v = view(r)
        v["items"] = it_by.get(r["id"], [])
        v["technicians"] = te_by.get(r["id"], [])
        v["tracker"] = tracker(r, ev_by.get(r["id"], []))
        data.append(v)
    return {"data": data, "statuses": STATUS_LABEL, "urgency": URGENCY_LABEL, "kinds": KINDS, "sla": SLA_HOURS,
            "categories": categories(False), "technicians": [t for t in technicians() if t["isActive"]]}


def team_full(request_id: str) -> dict:
    row = get_row(request_id)
    out = full(request_id)
    # every other request of the same branch or PIC (open first): one look at what is already asked
    rows = db.fetch(f"""SELECT {COLS} FROM {P}.de_request r WHERE r.id::text <> %s AND r.status <> 'DRAFT'
                        AND (r.branch_code = %s OR r.user_id::text = %s)
                        ORDER BY r.status = ANY(%s) DESC, r.submitted_at DESC LIMIT 30""",
                    (request_id, row["branch_code"], row["user_id"], list(OPEN)))
    out["related"] = [{**view(r), "items": items_of(r["id"])} for r in rows]
    out["allowed"] = allowed_next(row)
    return out


def allowed_next(row: dict) -> list[str]:
    if row["status"] == "ON_HOLD":
        return [row["hold_from"], "REJECTED"] if row["hold_from"] else ["REJECTED"]
    return list(FLOW.get(row["status"], ()))


def change_status(user: dict, request_id: str, to: str, note: Optional[str]) -> dict:
    row = get_row(request_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if to not in allowed_next(row):
        raise DeError(f"Status {STATUS_LABEL[row['status']]} tidak bisa langsung ke {STATUS_LABEL.get(to, to)}")
    note = (note or "").strip()[:2000] or None
    if to in NOTE_REQUIRED and not note:
        raise DeError(f"Alasan wajib diisi untuk {STATUS_LABEL[to]}")
    active = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_assignment WHERE request_id::text = %s AND status = 'ACTIVE'", (request_id,))["n"]
    if to == "IN_PROGRESS" and not active:
        raise DeError("Tugaskan tukang dulu sebelum mulai dikerjakan")
    if to == "DONE" and not db.fetchrow(f"SELECT 1 AS x FROM {P}.de_media WHERE request_id::text = %s AND kind = 'AFTER'", (request_id,)):
        raise DeError("Lampirkan minimal satu foto hasil pekerjaan sebelum Done")
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_request SET status = %s, hold_from = %s, updated_at = now(),
                         closed_at = CASE WHEN %s IN ('DONE', 'REJECTED') THEN now() END WHERE id::text = %s""",
                     (to, row["status"] if to == "ON_HOLD" else None, to, request_id))
        if to in ("DONE", "REJECTED"):
            conn.execute(f"""UPDATE {P}.de_assignment SET status = CASE WHEN %s = 'DONE' THEN 'DONE' ELSE 'RELEASED' END,
                             released_at = now(), released_note = %s WHERE request_id::text = %s AND status = 'ACTIVE'""",
                         (to, None if to == "DONE" else "Pengajuan ditolak", request_id))
        _event(conn, request_id, user, "STATUS", row["status"], to, note)
    return team_full(request_id)


def add_note(user: dict, request_id: str, note: str) -> dict:
    note = (note or "").strip()[:2000]
    if not note:
        raise DeError("Catatan kosong")
    with db.transaction() as conn:
        _event(conn, request_id, user, "NOTE", None, None, note)
    return team_full(request_id)


def set_cost(user: dict, request_id: str, est: Optional[float], actual: Optional[float], vendor: Optional[str]) -> dict:
    row = get_row(request_id)
    if row is None:
        raise LookupError("Pengajuan tidak ditemukan")
    vendor = (vendor or "").strip()[:120] or None
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.de_request SET est_cost = %s, actual_cost = %s, vendor = %s, updated_at = now() WHERE id::text = %s",
                     (est, actual, vendor, request_id))
        _event(conn, request_id, user, "COST", None, None, None, {"estCost": est, "actualCost": actual, "vendor": vendor})
    return team_full(request_id)


def assign(user: dict, request_id: str, technician_id: int, scheduled_for: Optional[date], note: Optional[str]) -> dict:
    row = get_row(request_id)
    if row is None or row["status"] not in ("REVIEW", "ACCEPTED", "IN_PROGRESS", "ON_HOLD"):
        raise DeError("Tukang ditugaskan setelah pengajuan di-review (Review, Accept atau Dikerjakan)")
    tech = db.fetchrow(f"SELECT id, name FROM {P}.de_technician WHERE id = %s AND is_active", (technician_id,))
    if tech is None:
        raise DeError("Tukang tidak ditemukan atau nonaktif")
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.de_assignment WHERE request_id::text = %s AND technician_id = %s AND status = 'ACTIVE'",
                   (request_id, technician_id)):
        raise DeError(f"{tech['name']} sudah ditugaskan di pengajuan ini")
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.de_assignment (request_id, technician_id, scheduled_for, note, assigned_by)
                         VALUES (%s, %s, %s, %s, %s)""", (request_id, technician_id, scheduled_for, (note or "").strip()[:500] or None,
                                                          user.get("full_name") or user["username"]))
        _event(conn, request_id, user, "ASSIGN", None, None, (note or "").strip()[:500] or None,
               {"technician": tech["name"], "scheduledFor": _iso(scheduled_for)})
    return team_full(request_id)


def _release(conn, user: dict, assignment: dict, note: str, kind: str = "RELEASE", details: Optional[dict] = None) -> None:
    conn.execute(f"UPDATE {P}.de_assignment SET status = 'RELEASED', released_at = now(), released_note = %s WHERE id = %s",
                 (note, assignment["id"]))
    _event(conn, assignment["request_id"], user, kind, None, None, note, {"technician": assignment["name"], **(details or {})})
    left = conn.execute(f"SELECT count(*)::int AS n FROM {P}.de_assignment WHERE request_id = %s AND status = 'ACTIVE'",
                        (assignment["request_id"],)).fetchone()["n"]
    st = conn.execute(f"SELECT status FROM {P}.de_request WHERE id = %s", (assignment["request_id"],)).fetchone()["status"]
    if not left and st == "IN_PROGRESS":  # nobody left on the job: back to accepted, waiting for a technician
        conn.execute(f"UPDATE {P}.de_request SET status = 'ACCEPTED', updated_at = now() WHERE id = %s", (assignment["request_id"],))
        _event(conn, assignment["request_id"], user, "STATUS", "IN_PROGRESS", "ACCEPTED", "Menunggu tukang pengganti")


def _assignment(assignment_id: int) -> dict:
    a = db.fetchrow(f"""SELECT a.id, a.request_id, a.request_id::text AS rid, a.technician_id, a.status, t.name, r.number
                        FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                        JOIN {P}.de_request r ON r.id = a.request_id WHERE a.id = %s""", (assignment_id,))
    if a is None or a["status"] != "ACTIVE":
        raise DeError("Penugasan tidak aktif")
    return a


def release(user: dict, assignment_id: int, note: str) -> dict:
    note = (note or "").strip()[:500]
    if not note:
        raise DeError("Alasan wajib diisi untuk melepas tukang")
    a = _assignment(assignment_id)
    with db.transaction() as conn:
        _release(conn, user, a, note)
    return team_full(a["rid"])


def swap(user: dict, assignment_id: int, to_request_id: str, scheduled_for: Optional[date], note: str) -> dict:
    """Move a technician from their current request to one that needs them first (priority): released there with the
    reason, assigned here; both timelines show the swap."""
    note = (note or "").strip()[:500]
    if not note:
        raise DeError("Alasan pergeseran tukang wajib diisi")
    a = _assignment(assignment_id)
    target = get_row(to_request_id)
    if target is None or target["status"] not in ("REVIEW", "ACCEPTED", "IN_PROGRESS", "ON_HOLD"):
        raise DeError("Pengajuan tujuan harus sudah di-review (Review, Accept atau Dikerjakan)")
    if target["id"] == a["rid"]:
        raise DeError("Pengajuan tujuan sama dengan pengajuan asal")
    with db.transaction() as conn:
        _release(conn, user, a, note, "SWAP", {"to": target["number"]})
        conn.execute(f"""INSERT INTO {P}.de_assignment (request_id, technician_id, scheduled_for, note, assigned_by)
                         VALUES (%s, %s, %s, %s, %s)""", (to_request_id, a["technician_id"], scheduled_for, note,
                                                          user.get("full_name") or user["username"]))
        _event(conn, to_request_id, user, "SWAP", None, None, note,
               {"technician": a["name"], "from": a["number"], "scheduledFor": _iso(scheduled_for)})
    return team_full(to_request_id)


def mark_duplicate(user: dict, request_id: str, of_id: str, note: str) -> dict:
    note = (note or "").strip()[:2000]
    if not note:
        raise DeError("Alasan wajib diisi untuk menandai duplikat")
    row, of = get_row(request_id), get_row(of_id)
    if row is None or of is None or of["status"] == "DRAFT" or row["id"] == of["id"]:
        raise DeError("Pengajuan asal tidak ditemukan")
    if row["status"] not in OPEN:
        raise DeError("Hanya pengajuan yang masih berjalan yang bisa ditandai duplikat")
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_request SET status = 'REJECTED', duplicate_of = %s, closed_at = now(), updated_at = now()
                         WHERE id::text = %s""", (of_id, request_id))
        conn.execute(f"""UPDATE {P}.de_assignment SET status = 'RELEASED', released_at = now(), released_note = 'Duplikat'
                         WHERE request_id::text = %s AND status = 'ACTIVE'""", (request_id,))
        _event(conn, request_id, user, "DUPLICATE", row["status"], "REJECTED", note, {"of": of["number"]})
    return team_full(request_id)


def renew_link(request_id: str) -> dict:
    row = db.fetchrow(f"""UPDATE {P}.de_request SET share_token = COALESCE(share_token, %s), share_expires_at = now() + %s::interval
                          WHERE id::text = %s AND status <> 'DRAFT' RETURNING share_token, share_expires_at""",
                      (secrets.token_urlsafe(24), f"{get_settings().share_link_days} days", request_id))
    if row is None:
        raise LookupError("Pengajuan tidak ditemukan")
    return {"token": row["share_token"], "expiresAt": _iso(row["share_expires_at"])}


def by_token(token: str) -> Optional[dict]:
    if not token or len(token) > 64:
        return None
    row = db.fetchrow(f"SELECT {COLS} FROM {P}.de_request r WHERE r.share_token = %s AND r.share_expires_at > now()", (token,))
    return row


def signature_of(request_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT signature, signature_type, user_id::text, branch_code FROM {P}.de_request WHERE id::text = %s AND signature IS NOT NULL",
                       (request_id,))
