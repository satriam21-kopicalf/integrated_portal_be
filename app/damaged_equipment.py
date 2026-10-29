"""Form → Damaged Equipment and Monitoring → Damaged Equipment (2026-10-21, migration 027).

A PIC requests, for one of their branches, either a REPAIR (Maintenance: building & furniture, heavy tools …) or a
PROCUREMENT (decoration, tools & heavy tools to add). A request is a draft until it is sent: items (category from the
master of 6 groups, name, qty, location, urgency), an analysis, up to MAX_PHOTOS camera photos and one video of at most
MAX_VIDEO_BYTES, a preview and the PIC's signature (required). Sending gives it a number (DE-YYMM-NNNN), a PDF and a
share link (read-only, no login, SHARE days).

The team moves it through FLOW: PENDING → VALIDATION → REVIEW → ACCEPTED → IN_PROGRESS → HANDOVER → DONE, with
ON_HOLD (resumes where it stopped) and REJECTED; a note is required to hold, reject, swap or mark a duplicate.
Technicians are assigned (several per request, all INTERNAL or all VENDOR: the request's mode) and can be moved to a
request that needs them first. INTERNAL: the technician sends the work photos through a link (AFTER) before the
handover. VENDOR: an estimate before Accept / In progress, the actual cost and an invoice before Done. HANDOVER (migration
030): the PIC confirms at the outlet with 1-5 camera photos, their signature and, for a vendor, the vendor's signature;
then the team processes Done. The due time comes from the urgency and can be adjusted by the team (with a reason, shown
to the PIC). Every step is an event: the timeline the PIC follows and the time tracker (time per stage, on hold apart).

To avoid double requests the form shows the open requests of the branch, and an item already open there is flagged;
the team can close a request as a duplicate of another one.
"""
import hashlib
import json
import math

import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
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
MAX_HANDOVER = 5           # PIC photos at the handover
MAX_MONITOR = 10           # PIC photos while the work runs
MAX_WORK_PHOTOS = 5        # photos per technician link
WORK_LINK_MINUTES = 60    # a technician link is valid 1 hour
MAX_INVOICES = 10
MAX_INVOICE_BYTES = 10 * 1024 * 1024
WAGE_TYPES = ("HOURLY", "DAILY", "WEEKLY", "MONTHLY", "PER_JOB")  # PER_JOB = borongan (one amount per request)
WAGE_UNIT = {"HOURLY": "jam", "DAILY": "hari", "WEEKLY": "minggu", "MONTHLY": "hari", "PER_JOB": "pekerjaan"}
COST_KINDS = ("MATERIAL", "SERVICE", "TRANSPORT", "OTHER")
MAX_VIDEO_BYTES = 10 * 1024 * 1024
MAX_ITEMS = 50
KINDS = {"REPAIR": "Maintenance", "PROCUREMENT": "Pengadaan (Dekorasi, Tools & Merchandise)"}
MERCH_GROUP = "Merchandise"  # category group whose items come from the ESB menu (MERCH; sizes MERCH SIZE)
URGENCY = ("CRITICAL", "HIGH", "NORMAL", "LOW", "PLANNED")  # most urgent first
URGENCY_LABEL = {"CRITICAL": "Kritis", "HIGH": "Tinggi", "NORMAL": "Normal", "LOW": "Rendah", "PLANNED": "Dijadwalkan"}
# hours from sending to done (on hold not counted); PLANNED has no target
SLA_HOURS = {"CRITICAL": 24, "HIGH": 72, "NORMAL": 168, "LOW": 336}
CAUSES = ("WEAR", "MISUSE", "AGE", "OTHER")
STATUS_LABEL = {"DRAFT": "Draf", "PENDING": "Pending", "VALIDATION": "Validasi", "REVIEW": "Review", "ON_HOLD": "On hold",
                "ACCEPTED": "Accept", "IN_PROGRESS": "Dikerjakan", "HANDOVER": "Serah terima", "DONE": "Done", "REJECTED": "Reject"}
OPEN = ("PENDING", "VALIDATION", "REVIEW", "ON_HOLD", "ACCEPTED", "IN_PROGRESS", "HANDOVER")
STEPS = ("PENDING", "VALIDATION", "REVIEW", "ACCEPTED", "IN_PROGRESS", "HANDOVER", "DONE")
FLOW = {
    "PENDING": ("VALIDATION", "ON_HOLD", "REJECTED"),
    "VALIDATION": ("REVIEW", "ON_HOLD", "REJECTED"),
    "REVIEW": ("ACCEPTED", "ON_HOLD", "REJECTED"),
    "ACCEPTED": ("IN_PROGRESS", "ON_HOLD", "REJECTED"),
    "IN_PROGRESS": ("HANDOVER", "ON_HOLD"),
    "HANDOVER": ("DONE", "IN_PROGRESS", "ON_HOLD"),  # back to In progress: the work is redone
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
    rows = db.fetch(f"""SELECT id, grp, name, default_urgency, examples, sort, is_active, branch_groups FROM {P}.de_category
                        {"WHERE is_active" if active_only else ""} ORDER BY sort, name""")
    return [{"id": r["id"], "group": r["grp"], "name": r["name"], "defaultUrgency": r["default_urgency"],
             "examples": list(r["examples"] or []), "sort": r["sort"], "isActive": r["is_active"],
             "branchGroups": list(r["branch_groups"] or [])} for r in rows]  # [] = every branch


def save_category(category_id: Optional[int], body: dict) -> dict:
    if body.get("defaultUrgency") not in URGENCY:
        raise DeError("Urgensi tidak dikenal")
    examples = [e.strip()[:80] for e in body.get("examples") or [] if e and e.strip()][:30]
    groups = sorted({str(g)[:40] for g in body.get("branchGroups") or [] if g})[:20]
    params = (body["group"].strip()[:60], body["name"].strip()[:80], body["defaultUrgency"], examples,
              int(body.get("sort") or 0), bool(body.get("isActive", True)), groups)
    try:
        if category_id is None:
            row = db.fetchrow(f"""INSERT INTO {P}.de_category (grp, name, default_urgency, examples, sort, is_active, branch_groups)
                                  VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""", params)
        else:
            row = db.fetchrow(f"""UPDATE {P}.de_category SET grp = %s, name = %s, default_urgency = %s, examples = %s, sort = %s,
                                  is_active = %s, branch_groups = %s, updated_at = now() WHERE id = %s RETURNING id""", (*params, category_id))
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
        raise DeError("Jenis upah harus per jam, harian, mingguan, bulanan atau borongan")
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


def _store(request_id: str, kind: str, data: bytes, mime: str, ext: str, user: Optional[dict], file_name: Optional[str] = None,
           taken_at: Optional[datetime] = None, lat: Optional[float] = None, lng: Optional[float] = None, note: Optional[str] = None,
           by_name: Optional[str] = None, work_link_id: Optional[int] = None) -> None:
    """user = the portal user uploading; a technician's link has none (by_name + work_link_id instead)."""
    media_id = str(uuid.uuid4())
    rel = f"de/{_now():%Y/%m}/{media_id}.{ext}"
    path = media_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.fetchrow(f"""INSERT INTO {P}.de_media (id, request_id, kind, storage_path, mime, bytes, sha256, file_name, taken_at, latitude,
                    longitude, uploaded_by, uploaded_by_name, note, work_link_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (media_id, request_id, kind, rel, mime, len(data), hashlib.sha256(data).hexdigest(), file_name, taken_at, lat, lng,
                 user["id"] if user else None, by_name or ((user.get("full_name") or user["username"]) if user else None), note, work_link_id))


def media_of(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id::text, kind, mime, bytes, file_name, taken_at, uploaded_at, uploaded_by_name, latitude, longitude, note,
                        work_link_id FROM {P}.de_media WHERE request_id::text = %s ORDER BY uploaded_at""", (request_id,))
    return [{"id": r["id"], "kind": r["kind"], "mime": r["mime"], "bytes": r["bytes"], "fileName": r["file_name"],
             "takenAt": _iso(r["taken_at"]), "uploadedAt": _iso(r["uploaded_at"]), "uploadedBy": r["uploaded_by_name"], "note": r["note"],
             "byTechnician": r["work_link_id"] is not None} for r in rows]


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


def without_invoices(r: dict) -> dict:
    """The request without its invoices and the events naming them (the share link and its PDF)."""
    return {**r, "media": [m for m in r.get("media", []) if m["kind"] != "INVOICE"],
            "events": [e for e in r.get("events", []) if not (e.get("details") or {}).get("invoice")]}


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
    r.duplicate_of::text, r.created_at, r.updated_at, r.submitted_at, r.closed_at, r.target_at, r.target_note, r.target_by,
    r.target_set_at, r.target_changes, r.mode, r.handover_requested_at, r.handover_at, r.handover_note, r.handover_distance_m,
    r.handover_outside, r.pic_sign_name, (r.pic_sign IS NOT NULL) AS has_pic_sign, r.vendor_sign_name,
    (r.vendor_sign IS NOT NULL) AS has_vendor_sign, r.approved_by, r.approval_step,
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
    """Time per status from sending to now / closing, on hold kept apart. Due time: the team's adjusted target, else
    sending + the urgency's hours + the time on hold."""
    sla = SLA_HOURS.get(row["urgency"])
    adjusted = row.get("target_at")
    if not row["submitted_at"]:
        return {"stages": [], "totalHours": None, "holdHours": 0, "workHours": None, "slaHours": sla, "dueAt": _iso(adjusted),
                "targetAdjusted": adjusted is not None, "overdue": False, "progress": 0}
    marks = [(datetime.fromisoformat(e["at"]), e["to"]) for e in events if e["kind"] in ("SUBMIT", "STATUS") and e["to"]]
    end = row["closed_at"] or _now()
    stages: dict[str, float] = {}
    for (start, st), nxt in zip(marks, [m[0] for m in marks[1:]] + [end]):
        stages[st] = stages.get(st, 0.0) + max(0.0, (nxt - start).total_seconds() / 3600)
    total = max(0.0, (end - row["submitted_at"]).total_seconds() / 3600)
    hold = stages.get("ON_HOLD", 0.0)
    for st in ("DONE", "REJECTED"):
        stages.pop(st, None)
    due = adjusted or (row["submitted_at"] + timedelta(hours=sla + hold) if sla else None)
    target_h = None if due is None else max(0.0, (due - row["submitted_at"]).total_seconds() / 3600 - hold)
    current = row["hold_from"] if row["status"] == "ON_HOLD" else row["status"]
    progress = 100 if row["status"] == "DONE" else (round(STEPS.index(current) * 100 / (len(STEPS) - 1)) if current in STEPS else 0)
    late = due is not None and end > due
    return {"stages": [{"status": s_, "label": STATUS_LABEL[s_], "hours": round(h, 2)} for s_, h in stages.items()],
            "totalHours": round(total, 2), "holdHours": round(hold, 2), "workHours": round(total - hold, 2),
            "slaHours": None if target_h is None else round(target_h, 2), "dueAt": _iso(due), "targetAdjusted": adjusted is not None,
            "overdue": bool(late and row["status"] in OPEN), "late": bool(late and row["status"] == "DONE"), "progress": progress}


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
        "mode": row.get("mode"),
        "target": {"at": _iso(row.get("target_at")), "note": row.get("target_note"), "by": row.get("target_by"),
                   "setAt": _iso(row.get("target_set_at")), "changes": row.get("target_changes") or 0} if row.get("target_at") else None,
        "handover": {"requestedAt": _iso(row.get("handover_requested_at")), "at": _iso(row.get("handover_at")), "note": row.get("handover_note"),
                     "distanceM": row.get("handover_distance_m"), "outsideRadius": row.get("handover_outside"),
                     "picName": row.get("pic_sign_name"), "hasPicSign": row.get("has_pic_sign"),
                     "vendorName": row.get("vendor_sign_name"), "hasVendorSign": row.get("has_vendor_sign"),
                     "approvedBy": row.get("approved_by")} if row.get("handover_requested_at") else None,
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
    from app import branch_groups as bg
    groups = {b["code"]: b["group_id"] for b in bg.outlets()}
    branches = branch_options(user)
    return {"user": {"id": str(user["id"]), "name": user.get("full_name") or user["username"]}, "branches": branches,
            "branchGroups": {b["code"]: groups.get(b["code"]) for b in branches},
            "categories": categories(), "kinds": KINDS, "urgency": URGENCY_LABEL, "requests": out, "merchandise": merchandise(),
            "limits": {"photos": MAX_PHOTOS, "videoBytes": MAX_VIDEO_BYTES, "items": MAX_ITEMS}}


def merchandise() -> dict:
    """The merchandise sold in the outlets (ESB menu category MERCH, sold in the last 12 months) and the sizes (MERCH SIZE),
    to pick in Procurement; a T-shirt takes a size."""
    rows = db.fetch(f"""SELECT category_detail, menu_name, sum(qty) AS qty FROM {P}.agg_menu_monthly
                        WHERE category_detail IN ('MERCH', 'MERCH SIZE') AND month >= (date_trunc('month', now()) - interval '12 months')::date
                        GROUP BY 1, 2 ORDER BY 1, 2""")
    order = ["SMALL", "MEDIUM", "LARGE", "EXTRA LARGE", "DOUBLE EXTRA LARGE", "TRIPLE EXTRA LARGE"]
    sizes = sorted({r["menu_name"].strip() for r in rows if r["category_detail"] == "MERCH SIZE"},
                   key=lambda n: (order.index(n.upper()) if n.upper() in order else 99, n))
    items = [{"name": r["menu_name"].strip(), "sized": "SHIRT" in r["menu_name"].upper()} for r in rows if r["category_detail"] == "MERCH"]
    return {"items": items, "sizes": sizes}


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
    if (get_row(request_id) or {}).get("status") in OPEN:
        recompute(request_id)  # the labour of a running job grows with the time
    row = get_row(request_id)
    out = full(request_id)
    # every other request of the same branch or PIC (open first): one look at what is already asked
    rows = db.fetch(f"""SELECT {COLS} FROM {P}.de_request r WHERE r.id::text <> %s AND r.status <> 'DRAFT'
                        AND (r.branch_code = %s OR r.user_id::text = %s)
                        ORDER BY r.status = ANY(%s) DESC, r.submitted_at DESC LIMIT 30""",
                    (request_id, row["branch_code"], row["user_id"], list(OPEN)))
    out["related"] = [{**view(r), "items": items_of(r["id"])} for r in rows]
    out["allowed"] = allowed_next(row)
    out["approval"] = approval_state(row) if row["status"] == "REVIEW" else None
    out["workLinks"] = work_links(request_id)
    out["costItems"] = cost_items(request_id)
    out["labor"] = labor(request_id)
    return out


def allowed_next(row: dict) -> list[str]:
    if row["status"] == "ON_HOLD":
        return [row["hold_from"], "REJECTED"] if row["hold_from"] else ["REJECTED"]
    return list(FLOW.get(row["status"], ()))


def approval_state(row: dict) -> Optional[dict]:
    """The approval step a request in Review waits at (Review → Accept, set by management like Form Fund; 2026-10-29).
    None when its kind has no step: any monitoring account accepts it, as before."""
    from app import pengajuan as pg
    steps = pg._approvals(pg.flow(row["kind"], "de"))
    if not steps:
        return None
    at = min(max(row.get("approval_step") or 1, 1), len(steps))
    step = steps[at - 1]
    return {"position": at, "total": len(steps), "label": step["label"], "users": [u["name"] for u in step["users"]],
            "userIds": [u["id"] for u in step["users"]], "steps": [{"label": s["label"], "users": [u["name"] for u in s["users"]]} for s in steps]}


def change_status(user: dict, request_id: str, to: str, note: Optional[str]) -> dict:
    recompute(request_id)
    row = get_row(request_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if to not in allowed_next(row):
        raise DeError(f"Status {STATUS_LABEL[row['status']]} tidak bisa langsung ke {STATUS_LABEL.get(to, to)}")
    note = (note or "").strip()[:2000] or None
    if to in NOTE_REQUIRED and not note:
        raise DeError(f"Alasan wajib diisi untuk {STATUS_LABEL[to]}")
    # Review → Accept / Reject: the approver of the step waiting; the steps before the last keep it in Review
    approval = approval_state(row) if row["status"] == "REVIEW" and to in ("ACCEPTED", "REJECTED") else None
    if approval is not None:
        from app import pengajuan as pg
        step = {"users": [{"id": i} for i in approval["userIds"]]}
        if not pg.takes(user, step):
            raise PermissionError(f"Tahap {approval['label']} bukan bagian Anda (approver: {', '.join(approval['users']) or 'superadmin'})")
        if to == "ACCEPTED" and approval["position"] < approval["total"]:
            with db.transaction() as conn:
                conn.execute(f"UPDATE {P}.de_request SET approval_step = %s, updated_at = now() WHERE id::text = %s",
                             (approval["position"] + 1, request_id))
                _event(conn, request_id, user, "STATUS", "REVIEW", "REVIEW", note, {"action": "APPROVE", "step": approval["label"]})
            return team_full(request_id)
    active = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_assignment WHERE request_id::text = %s AND status = 'ACTIVE'", (request_id,))["n"]
    vendor = row.get("mode") == "VENDOR" and active > 0
    count = lambda kind: db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id::text = %s AND kind = %s",  # noqa: E731
                                     (request_id, kind))["n"]
    if to in ("IN_PROGRESS", "HANDOVER") and not active:
        raise DeError("Tugaskan tukang dulu sebelum mulai dikerjakan")
    if to in ("ACCEPTED", "IN_PROGRESS") and vendor and row["est_cost"] is None:
        raise DeError("Isi estimasi biaya vendor dulu (Biaya & invoice)")
    if to == "HANDOVER" and row.get("mode") == "INTERNAL" and not count("AFTER"):
        raise DeError("Foto hasil pekerjaan dari tukang belum masuk: kirim link foto ke tukang dulu")
    if to == "DONE":
        if not row.get("handover_at"):
            raise DeError("Serah terima dari PIC belum dikirim")
        if row.get("mode") == "VENDOR" and (row["actual_cost"] is None or not count("INVOICE")):
            raise DeError("Pekerjaan vendor: isi biaya aktual dan lampirkan invoice sebelum Done")
    reopen = to == "IN_PROGRESS" and row["status"] == "HANDOVER"
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_request SET status = %s, hold_from = %s, updated_at = now(),
                         closed_at = CASE WHEN %s IN ('DONE', 'REJECTED') THEN now() END WHERE id::text = %s""",
                     (to, row["status"] if to == "ON_HOLD" else None, to, request_id))
        if to == "HANDOVER" and row["status"] != "ON_HOLD":
            conn.execute(f"UPDATE {P}.de_request SET handover_requested_at = now() WHERE id::text = %s", (request_id,))
        if reopen:  # redo: a new handover is needed (the photos stay as history)
            conn.execute(f"""UPDATE {P}.de_request SET handover_at = NULL, pic_sign = NULL, pic_sign_type = NULL, pic_sign_name = NULL,
                             vendor_sign = NULL, vendor_sign_type = NULL, vendor_sign_name = NULL, handover_note = NULL WHERE id::text = %s""",
                         (request_id,))
        if to == "DONE":
            conn.execute(f"UPDATE {P}.de_request SET approved_by = %s WHERE id::text = %s", (user.get("full_name") or user["username"], request_id))
        if to == "REVIEW" and row["status"] != "ON_HOLD":  # a new review starts at the first approval step
            conn.execute(f"UPDATE {P}.de_request SET approval_step = 1 WHERE id::text = %s", (request_id,))
        if row["status"] == "IN_PROGRESS" or to in ("DONE", "REJECTED"):  # the work stops: open technician links close
            conn.execute(f"""UPDATE {P}.de_work_link SET revoked_at = now() WHERE request_id::text = %s AND submitted_at IS NULL
                             AND revoked_at IS NULL""", (request_id,))
        if to in ("DONE", "REJECTED"):
            conn.execute(f"""UPDATE {P}.de_assignment SET status = CASE WHEN %s = 'DONE' THEN 'DONE' ELSE 'RELEASED' END,
                             released_at = now(), released_note = %s WHERE request_id::text = %s AND status = 'ACTIVE'""",
                         (to, None if to == "DONE" else "Pengajuan ditolak", request_id))
        _event(conn, request_id, user, "STATUS", row["status"], to, note)
    recompute(request_id)  # Done / reject: the labour stops here
    return team_full(request_id)


def add_note(user: dict, request_id: str, note: str) -> dict:
    note = (note or "").strip()[:2000]
    if not note:
        raise DeError("Catatan kosong")
    with db.transaction() as conn:
        _event(conn, request_id, user, "NOTE", None, None, note)
    return team_full(request_id)


def assign(user: dict, request_id: str, technician_id: int, scheduled_for: Optional[date], note: Optional[str]) -> dict:
    row = get_row(request_id)
    if row is None or row["status"] not in ("REVIEW", "ACCEPTED", "IN_PROGRESS", "ON_HOLD"):
        raise DeError("Tukang ditugaskan setelah pengajuan di-review (Review, Accept atau Dikerjakan)")
    tech = db.fetchrow(f"SELECT id, name, kind FROM {P}.de_technician WHERE id = %s AND is_active", (technician_id,))
    if tech is None:
        raise DeError("Tukang tidak ditemukan atau nonaktif")
    other = db.fetchrow(f"""SELECT t.kind FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                            WHERE a.request_id::text = %s AND a.status = 'ACTIVE' AND t.kind <> %s LIMIT 1""", (request_id, tech["kind"]))
    if other:
        raise DeError("Satu pengajuan hanya untuk tukang internal atau vendor; lepas tukang " +
                      ("internal" if other["kind"] == "INTERNAL" else "vendor") + " dulu")
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.de_assignment WHERE request_id::text = %s AND technician_id = %s AND status = 'ACTIVE'",
                   (request_id, technician_id)):
        raise DeError(f"{tech['name']} sudah ditugaskan di pengajuan ini")
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.de_assignment (request_id, technician_id, scheduled_for, note, assigned_by)
                         VALUES (%s, %s, %s, %s, %s)""", (request_id, technician_id, scheduled_for, (note or "").strip()[:500] or None,
                                                          user.get("full_name") or user["username"]))
        conn.execute(f"UPDATE {P}.de_request SET mode = %s WHERE id::text = %s", (tech["kind"], request_id))
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
        kind = conn.execute(f"SELECT kind FROM {P}.de_technician WHERE id = %s", (a["technician_id"],)).fetchone()["kind"]
        clash = conn.execute(f"""SELECT 1 FROM {P}.de_assignment x JOIN {P}.de_technician t ON t.id = x.technician_id
                                 WHERE x.request_id::text = %s AND x.status = 'ACTIVE' AND t.kind <> %s""", (to_request_id, kind)).fetchone()
        if clash:
            raise DeError("Pengajuan tujuan memakai jenis tukang lain (internal / vendor)")
        conn.execute(f"""INSERT INTO {P}.de_assignment (request_id, technician_id, scheduled_for, note, assigned_by)
                         VALUES (%s, %s, %s, %s, %s)""", (to_request_id, a["technician_id"], scheduled_for, note,
                                                          user.get("full_name") or user["username"]))
        conn.execute(f"UPDATE {P}.de_request SET mode = %s WHERE id::text = %s", (kind, to_request_id))
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


# ---------------------------------------------------------------- technician photo link (no sign-in, 1 hour)

def _link_state(r: dict) -> str:
    if r["submitted_at"] is not None:
        return "SENT"
    if r["revoked_at"] is not None:
        return "CLOSED"
    return "OPEN" if r["expires_at"] > _now() else "EXPIRED"


def work_links(request_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT l.*, (SELECT count(*) FROM {P}.de_media m WHERE m.work_link_id = l.id)::int AS photos
                        FROM {P}.de_work_link l WHERE l.request_id::text = %s ORDER BY l.created_at DESC""", (request_id,))
    return [{"id": r["id"], "token": r["token"], "technician": r["technician_name"], "createdBy": r["created_by_name"],
             "createdAt": _iso(r["created_at"]), "expiresAt": _iso(r["expires_at"]), "submittedAt": _iso(r["submitted_at"]),
             "note": r["submit_note"], "photos": r["photos"], "state": _link_state(r)} for r in rows]


def create_work_link(user: dict, request_id: str, technician_id: Optional[int]) -> dict:
    """A new link for an assigned technician (the request must be In progress); an older open link of the request closes."""
    row = get_row(request_id)
    if row is None or row["status"] != "IN_PROGRESS":
        raise DeError("Link foto untuk tukang dibuat saat pengajuan berstatus Dikerjakan")
    techs = db.fetch(f"""SELECT t.id, t.name FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                         WHERE a.request_id::text = %s AND a.status = 'ACTIVE' AND t.kind = 'INTERNAL' ORDER BY a.id""", (request_id,))
    if row.get("mode") == "VENDOR":
        raise DeError("Link foto untuk tukang internal; pekerjaan vendor difoto PIC saat serah terima")
    tech = next((t for t in techs if technician_id is None or t["id"] == technician_id), None)
    if tech is None:
        raise DeError("Pilih tukang yang ditugaskan di pengajuan ini")
    token = secrets.token_urlsafe(24)
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_work_link SET revoked_at = now() WHERE request_id::text = %s AND submitted_at IS NULL
                         AND revoked_at IS NULL""", (request_id,))
        conn.execute(f"""INSERT INTO {P}.de_work_link (token, request_id, technician_id, technician_name, created_by, created_by_name, expires_at)
                         VALUES (%s, %s, %s, %s, %s, %s, now() + %s::interval)""",
                     (token, request_id, tech["id"], tech["name"], user["id"], user.get("full_name") or user["username"],
                      f"{WORK_LINK_MINUTES} minutes"))
        _event(conn, request_id, user, "WORK", None, None, None, {"technician": tech["name"], "link": "created"})
    return team_full(request_id)


def revoke_work_link(user: dict, request_id: str, link_id: int) -> dict:
    db.fetchrow(f"""UPDATE {P}.de_work_link SET revoked_at = now() WHERE id = %s AND request_id::text = %s AND submitted_at IS NULL
                    AND revoked_at IS NULL RETURNING id""", (link_id, request_id))
    return team_full(request_id)


def _work(token: str, writable: bool = False) -> dict:
    """The link with its request: unknown, expired or closed → LookupError (a sent link stays readable until it expires)."""
    if not token or len(token) > 64:
        raise LookupError("Link tidak ditemukan")
    link = db.fetchrow(f"""SELECT l.*, r.status, r.number, r.kind, r.branch_code, r.branch_name, r.analysis, r.id::text AS rid
                           FROM {P}.de_work_link l JOIN {P}.de_request r ON r.id = l.request_id WHERE l.token = %s""", (token,))
    if link is None or link["revoked_at"] is not None or link["expires_at"] <= _now():
        raise LookupError("Link sudah tidak berlaku. Minta link baru ke tim maintenance.")
    if writable and (link["submitted_at"] is not None or link["status"] != "IN_PROGRESS"):
        raise DeError("Foto sudah dikirim; link ini tidak bisa diubah lagi")
    return link


def work_view(token: str) -> dict:
    link = _work(token)
    photos = db.fetch(f"SELECT id::text, taken_at, uploaded_at FROM {P}.de_media WHERE work_link_id = %s ORDER BY uploaded_at", (link["id"],))
    return {"number": link["number"], "kind": link["kind"], "kindLabel": KINDS[link["kind"]], "branchCode": link["branch_code"],
            "branchName": link["branch_name"], "analysis": link["analysis"], "technician": link["technician_name"],
            "items": [{k: i.get(k) for k in ("name", "qty", "category", "group", "location", "note", "urgency")} for i in items_of(link["rid"])],
            "expiresAt": _iso(link["expires_at"]), "submittedAt": _iso(link["submitted_at"]), "note": link["submit_note"],
            "photos": [{"id": ph["id"], "takenAt": _iso(ph["taken_at"] or ph["uploaded_at"])} for ph in photos], "max": MAX_WORK_PHOTOS}


def work_add_photo(token: str, photo: str, lat: Optional[float], lng: Optional[float], taken_at: Optional[datetime]) -> dict:
    link = _work(token, writable=True)
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE work_link_id = %s", (link["id"],))["n"]
    if n >= MAX_WORK_PHOTOS:
        raise DeError(f"Maksimal {MAX_WORK_PHOTOS} foto; hapus satu dulu")
    after = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id = %s AND kind = 'AFTER'", (link["request_id"],))["n"]
    if after >= MAX_AFTER:
        raise DeError(f"Foto hasil pekerjaan pengajuan ini sudah {MAX_AFTER}")
    mime, data = parse_photo(photo)
    _store(link["rid"], "AFTER", data, mime, {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg"), None,
           taken_at=taken_at, lat=lat, lng=lng, by_name=f"{link['technician_name']} (tukang)", work_link_id=link["id"])
    return work_view(token)


def work_delete_photo(token: str, media_id: str) -> dict:
    link = _work(token, writable=True)
    row = db.fetchrow(f"DELETE FROM {P}.de_media WHERE id::text = %s AND work_link_id = %s RETURNING storage_path", (media_id, link["id"]))
    if row:
        (media_root() / row["storage_path"]).unlink(missing_ok=True)
    return work_view(token)


def work_submit(token: str, note: Optional[str]) -> dict:
    link = _work(token, writable=True)
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE work_link_id = %s", (link["id"],))["n"]
    if n < 1:
        raise DeError("Ambil minimal 1 foto hasil pekerjaan")
    note = (note or "").strip()[:1000] or None
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.de_work_link SET submitted_at = now(), submit_note = %s WHERE id = %s", (note, link["id"]))
        conn.execute(f"""INSERT INTO {P}.de_event (request_id, actor_id, actor_name, kind, note, details)
                         VALUES (%s, NULL, %s, 'WORK', %s, %s::jsonb)""",
                     (link["request_id"], f"{link['technician_name']} (tukang)", note,
                      json.dumps({"technician": link["technician_name"], "link": "sent", "photos": n}, ensure_ascii=False)))
    return work_view(token)


def work_media(token: str, media_id: str) -> Optional[dict]:
    link = _work(token)
    if not db.fetchrow(f"SELECT 1 AS x FROM {P}.de_media WHERE id::text = %s AND work_link_id = %s", (media_id, link["id"])):
        return None
    return media_file(media_id)


# ---------------------------------------------------------------- target time (maintenance team)

def set_target(user: dict, request_id: str, due_at: datetime, note: str) -> dict:
    row = get_row(request_id)
    if row is None or row["status"] not in OPEN:
        raise DeError("Target waktu diubah selama pengajuan masih berjalan")
    note = (note or "").strip()[:500]
    if not note:
        raise DeError("Alasan penyesuaian target wajib diisi")
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=WIB)
    if due_at <= row["submitted_at"]:
        raise DeError("Target harus setelah waktu pengajuan dikirim")
    before = tracker(row, events_of(request_id))["dueAt"]
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_request SET target_at = %s, target_note = %s, target_by = %s, target_set_at = now(),
                         target_changes = target_changes + 1, updated_at = now() WHERE id::text = %s""",
                     (due_at, note, user.get("full_name") or user["username"], request_id))
        _event(conn, request_id, user, "TARGET", None, None, note, {"from": before, "to": _iso(due_at)})
    return team_full(request_id)


# ---------------------------------------------------------------- the PIC: photos while the work runs, handover

def own_sent(user: dict, request_id: str) -> dict:
    row = get_row(request_id)
    if row is None or row["user_id"] != str(user["id"]) or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    return row


def pic_photo(user: dict, request_id: str, kind: str, photo: str, lat: Optional[float], lng: Optional[float], taken_at: Optional[datetime]) -> dict:
    """MONITOR: while the technician works (In progress / Serah terima); HANDOVER: at the handover, before it is sent."""
    row = own_sent(user, request_id)
    if kind == "MONITOR" and row["status"] not in ("IN_PROGRESS", "HANDOVER"):
        raise DeError("Foto pemantauan diambil saat pengajuan sedang dikerjakan")
    if kind == "HANDOVER" and (row["status"] != "HANDOVER" or row["handover_at"]):
        raise DeError("Foto serah terima diambil saat tim maintenance meminta serah terima")
    limit = MAX_HANDOVER if kind == "HANDOVER" else MAX_MONITOR
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id::text = %s AND kind = %s", (request_id, kind))["n"]
    if n >= limit:
        raise DeError(f"Maksimal {limit} foto; hapus satu dulu")
    mime, data = parse_photo(photo)
    _store(request_id, kind, data, mime, {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg"), user,
           taken_at=taken_at, lat=lat, lng=lng)
    return full(request_id)


def pic_delete_photo(user: dict, request_id: str, media_id: str) -> dict:
    row = own_sent(user, request_id)
    if row["handover_at"] or row["status"] not in ("IN_PROGRESS", "HANDOVER"):
        raise DeError("Foto tidak bisa dihapus lagi")
    delete_media(request_id, media_id, ("MONITOR", "HANDOVER"))
    return full(request_id)


def submit_handover(user: dict, request_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float],
                    pic_signature: Optional[str], pic_name: Optional[str], vendor_signature: Optional[str], vendor_name: Optional[str],
                    note: Optional[str]) -> dict:
    row = own_sent(user, request_id)
    if row["status"] != "HANDOVER" or row["handover_at"]:
        raise DeError("Serah terima belum diminta tim maintenance atau sudah dikirim")
    n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.de_media WHERE request_id::text = %s AND kind = 'HANDOVER'", (request_id,))["n"]
    if n < 1:
        raise DeError("Ambil minimal 1 foto hasil pekerjaan di outlet")
    pic_name = " ".join((pic_name or "").split())[:120]
    if not pic_signature or not pic_name:
        raise DeError("Tanda tangan dan nama PIC wajib")
    pic_type, pic_sig = parse_photo(pic_signature)
    v_type = v_sig = None
    vendor_name = " ".join((vendor_name or "").split())[:120] or None
    if row.get("mode") == "VENDOR":
        if not vendor_signature or not vendor_name:
            raise DeError("Pekerjaan vendor: tanda tangan dan nama petugas vendor wajib")
        v_type, v_sig = parse_photo(vendor_signature)
    outlet = locations([row["branch_code"]]).get(row["branch_code"]) or {}
    j = judge(lat, lng, accuracy, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.de_request SET handover_at = now(), handover_note = %s, handover_latitude = %s, handover_longitude = %s,
                         handover_distance_m = %s, handover_outside = %s, pic_sign = %s, pic_sign_type = %s, pic_sign_name = %s,
                         vendor_sign = %s, vendor_sign_type = %s, vendor_sign_name = %s, updated_at = now() WHERE id::text = %s""",
                     ((note or "").strip()[:1000] or None, lat, lng, j["distance_m"], j["outside_radius"], pic_sig, pic_type, pic_name,
                      v_sig, v_type, vendor_name if v_sig else None, request_id))
        _event(conn, request_id, user, "HANDOVER", None, None, (note or "").strip()[:1000] or None,
               {"photos": n, "vendor": vendor_name if v_sig else None, "distanceM": j["distance_m"]})
    return full(request_id)


def handover_signature(request_id: str, who: str) -> Optional[dict]:
    col = "pic_sign" if who == "pic" else "vendor_sign"
    return db.fetchrow(f"SELECT {col} AS signature, {col}_type AS signature_type FROM {P}.de_request WHERE id::text = %s AND {col} IS NOT NULL",
                       (request_id,))


# ---------------------------------------------------------------- overview (analysis of every request)

def overview(date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    """Requests sent in the period: KPIs, weekly trend, status, time per stage, category groups / categories, branches,
    repeated damage, urgency vs target, internal vs vendor, reasons of reject / hold."""
    where = ["r.status <> 'DRAFT'", "(r.submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"a": date_from, "b": date_to}
    if branches is not None:
        where.append("r.branch_code = ANY(%(br)s)")
        params["br"] = branches
    rows = db.fetch(f"SELECT {COLS} FROM {P}.de_request r WHERE {' AND '.join(where)} ORDER BY r.submitted_at LIMIT 5000", params)
    ids = [r["id"] for r in rows]
    items = db.fetch(f"""SELECT i.request_id::text, c.grp, c.name AS category, i.name FROM {P}.de_item i
                         JOIN {P}.de_category c ON c.id = i.category_id WHERE i.request_id::text = ANY(%s)""", (ids,))
    events = db.fetch(f"""SELECT e.request_id::text, e.at, e.kind, e.to_status, e.note, r.number FROM {P}.de_event e
                          JOIN {P}.de_request r ON r.id = e.request_id WHERE e.request_id::text = ANY(%s) ORDER BY e.at, e.id""", (ids,))
    assign = db.fetch(f"""SELECT a.request_id::text, t.id, t.name, t.kind FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                          WHERE a.request_id::text = ANY(%s) AND a.status IN ('ACTIVE', 'DONE')""", (ids,))
    it_by, ev_by, as_by = {}, {}, {}
    for i in items:
        it_by.setdefault(i["request_id"], []).append(i)
    for e in events:
        ev_by.setdefault(e["request_id"], []).append({"at": _iso(e["at"]), "kind": e["kind"], "to": e["to_status"], "note": e["note"], "number": e["number"]})
    for a in assign:
        as_by.setdefault(a["request_id"], []).append(a)

    def cost(r):
        return _num(r["actual_cost"]) if r["actual_cost"] is not None else (_num(r["est_cost"]) or 0)

    reqs = []
    for r in rows:
        t = tracker(r, ev_by.get(r["id"], []))
        reqs.append({"row": r, "t": t, "items": it_by.get(r["id"], []), "techs": as_by.get(r["id"], []), "cost": cost(r) or 0})
    done = [x for x in reqs if x["row"]["status"] == "DONE"]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731

    weeks: dict[str, dict] = {}
    for x in reqs:
        d = x["row"]["submitted_at"].astimezone(WIB).date()
        wk = (d - timedelta(days=d.weekday())).isoformat()
        weeks.setdefault(wk, {"week": wk, "in": 0, "done": 0})["in"] += 1
    for x in done:
        d = x["row"]["closed_at"].astimezone(WIB).date()
        wk = (d - timedelta(days=d.weekday())).isoformat()
        weeks.setdefault(wk, {"week": wk, "in": 0, "done": 0})["done"] += 1

    stage_h: dict[str, list] = {}
    for x in reqs:
        for st in x["t"]["stages"]:
            stage_h.setdefault(st["status"], []).append(st["hours"])

    groups: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    for x in reqs:
        for key, bucket, field in ((None, groups, "grp"), (None, cats, "category")):
            for name in {i[field] for i in x["items"]}:
                g = bucket.setdefault(name, {"name": name, "count": 0, "open": 0, "hours": [], "cost": 0.0})
                g["count"] += 1
                g["open"] += x["row"]["status"] in OPEN
                g["cost"] += x["cost"]
                if x["row"]["status"] == "DONE":
                    g["hours"].append(x["t"]["workHours"])

    def finish(d):
        return sorted(({**v, "avgHours": avg(v.pop("hours"))} for v in d.values()), key=lambda v: -v["count"])

    branches_: dict[str, dict] = {}
    for x in reqs:
        b = branches_.setdefault(x["row"]["branch_code"], {"code": x["row"]["branch_code"], "name": x["row"]["branch_name"], "count": 0, "open": 0, "cost": 0.0})
        b["count"] += 1
        b["open"] += x["row"]["status"] in OPEN
        b["cost"] += x["cost"]

    seen: dict[tuple, list] = {}
    for x in reqs:
        for i in x["items"]:
            seen.setdefault((x["row"]["branch_code"], i["category"], i["name"].strip().lower()), []).append((x["row"]["submitted_at"], x["row"]["number"], i["name"], x["row"]["branch_name"]))
    repeats = []
    for (code, cat, _), hits in seen.items():
        hits.sort()
        close = [h for k, h in enumerate(hits) if any(abs((h[0] - o[0]).days) <= 90 for m, o in enumerate(hits) if m != k)]
        if len(close) >= 2:
            repeats.append({"branchCode": code, "branchName": close[0][3], "category": cat, "item": close[-1][2], "count": len(close),
                            "numbers": [h[1] for h in close]})
    repeats.sort(key=lambda v: -v["count"])

    urg = []
    for u in URGENCY:
        xs = [x for x in reqs if x["row"]["urgency"] == u]
        dn = [x for x in xs if x["row"]["status"] == "DONE"]
        on_time = [x for x in dn if not x["t"]["late"]]
        urg.append({"urgency": u, "label": URGENCY_LABEL[u], "count": len(xs), "done": len(dn), "onTime": len(on_time),
                    "pct": round(len(on_time) * 100 / len(dn)) if dn else None, "avgHours": avg([x["t"]["workHours"] for x in dn])})

    modes = {}
    for m in ("INTERNAL", "VENDOR"):
        xs = [x for x in reqs if x["row"].get("mode") == m]
        dn = [x for x in xs if x["row"]["status"] == "DONE"]
        modes[m] = {"jobs": len(xs), "done": len(dn), "avgHours": avg([x["t"]["workHours"] for x in dn]), "cost": sum(x["cost"] for x in xs)}
    tech: dict[int, dict] = {}
    for x in reqs:
        for a in {a["id"]: a for a in x["techs"]}.values():
            v = tech.setdefault(a["id"], {"name": a["name"], "kind": a["kind"], "jobs": 0, "done": 0, "hours": []})
            v["jobs"] += 1
            if x["row"]["status"] == "DONE":
                v["done"] += 1
                v["hours"].append(x["t"]["workHours"])
    techs = sorted(({**v, "avgHours": avg(v.pop("hours"))} for v in tech.values()), key=lambda v: -v["jobs"])

    reasons = [{"number": e["number"], "status": e["to"], "note": e["note"], "at": e["at"]}
               for x in reqs for e in ev_by.get(x["row"]["id"], []) if e["kind"] in ("STATUS", "DUPLICATE") and e["to"] in ("REJECTED", "ON_HOLD") and e["note"]]
    reasons.sort(key=lambda v: v["at"], reverse=True)
    status_counts = {st: sum(x["row"]["status"] == st for x in reqs) for st in STATUS_LABEL if st != "DRAFT"}
    return {
        "kpi": {"total": len(reqs), "open": sum(x["row"]["status"] in OPEN for x in reqs), "done": len(done),
                "rejected": status_counts["REJECTED"], "onHold": status_counts["ON_HOLD"],
                "avgHours": avg([x["t"]["workHours"] for x in done]),
                "onTimePct": round(sum(not x["t"]["late"] for x in done) * 100 / len(done)) if done else None,
                "overdue": sum(x["t"]["overdue"] for x in reqs), "cost": sum(x["cost"] for x in reqs),
                "targetAdjusted": sum(x["row"].get("target_changes") or 0 for x in reqs),
                "duplicates": sum(x["row"]["duplicate_of"] is not None for x in reqs),
                "repair": sum(x["row"]["kind"] == "REPAIR" for x in reqs), "procurement": sum(x["row"]["kind"] == "PROCUREMENT" for x in reqs)},
        "trend": sorted(weeks.values(), key=lambda v: v["week"]),
        "status": [{"status": st, "label": STATUS_LABEL[st], "count": n} for st, n in status_counts.items()],
        "stages": [{"status": st, "label": STATUS_LABEL[st], "avgHours": avg(stage_h.get(st, []))} for st in (*STEPS[:-1], "ON_HOLD") if stage_h.get(st)],
        "groups": finish(groups), "categories": finish(cats)[:10],
        "branches": sorted(branches_.values(), key=lambda v: -v["count"])[:10], "repeats": repeats[:10],
        "urgency": urg, "modes": modes, "technicians": techs[:10], "reasons": reasons[:8],
    }


# ---------------------------------------------------------------- cost lines & labour

def cost_items(request_id: str) -> list[dict]:
    rows = db.fetch(f"SELECT * FROM {P}.de_cost_item WHERE request_id::text = %s ORDER BY id", (request_id,))
    return [{"id": r["id"], "kind": r["kind"], "description": r["description"], "qty": _num(r["qty"]), "estAmount": _num(r["est_amount"]),
             "actualAmount": _num(r["actual_amount"]), "by": r["created_by"], "at": _iso(r["updated_at"])} for r in rows]


def _units(wage_type: str, hours: float) -> float:
    """Billable units of a job from the hours worked (rounded up): half hours, days, weeks; per job = 1."""
    if wage_type == "PER_JOB":
        return 1.0
    if hours <= 0:
        return 0.0
    if wage_type == "HOURLY":
        return math.ceil(hours * 2) / 2
    if wage_type == "WEEKLY":
        return float(math.ceil(hours / 168))
    return float(math.ceil(hours / 24))  # DAILY, MONTHLY (per working day: monthly / 26)


def labor(request_id: str) -> list[dict]:
    """The labour of every technician who worked on the request: from In progress (or the assignment, if later) to the
    release / Done / the handover request; amount = rate × units (monthly: rate / 26 per day), or the team's adjustment."""
    row = get_row(request_id)
    if row is None:
        return []
    start_ev = db.fetchrow(f"""SELECT min(at) AS at FROM {P}.de_event WHERE request_id::text = %s AND kind = 'STATUS' AND to_status = 'IN_PROGRESS'""",
                           (request_id,))["at"]
    rows = db.fetch(f"""SELECT a.id, a.status, a.assigned_at, a.released_at, a.labor_adjust, a.labor_note, t.name, t.kind, t.company,
                               t.wage_type, t.wage_amount FROM {P}.de_assignment a JOIN {P}.de_technician t ON t.id = a.technician_id
                        WHERE a.request_id::text = %s ORDER BY a.assigned_at""", (request_id,))
    stop = row["handover_requested_at"] if row.get("handover_requested_at") and row["status"] not in ("IN_PROGRESS",) else None
    out = []
    for a in rows:
        begin = max(a["assigned_at"], start_ev) if start_ev else None
        end = a["released_at"] or _now()
        if stop and a["status"] != "RELEASED":
            end = min(end, stop)
        hours = max(0.0, (end - begin).total_seconds() / 3600) if begin else 0.0
        if a["status"] == "RELEASED" and hours <= 0:
            continue
        rate = _num(a["wage_amount"]) or 0.0
        units = _units(a["wage_type"], hours) if (begin or a["wage_type"] == "PER_JOB") else 0.0
        computed = round(rate / 26 * units if a["wage_type"] == "MONTHLY" else rate * units, 2)
        adjust = _num(a["labor_adjust"])
        out.append({"assignmentId": a["id"], "technician": a["name"], "kind": a["kind"], "company": a["company"], "status": a["status"],
                    "wageType": a["wage_type"], "rate": rate, "hours": round(hours, 2), "units": units, "unit": WAGE_UNIT[a["wage_type"]],
                    "computed": computed, "adjust": adjust, "note": a["labor_note"], "amount": adjust if adjust is not None else computed})
    return out


def recompute(request_id: str) -> None:
    """The request's estimated cost = the estimates of the lines; actual = the actual lines + the labour."""
    items = cost_items(request_id)
    lab = sum(x["amount"] or 0 for x in labor(request_id))
    ests = [i["estAmount"] for i in items if i["estAmount"] is not None]
    acts = [i["actualAmount"] for i in items if i["actualAmount"] is not None]
    est = round(sum(ests), 2) if ests else None
    actual = round(sum(acts) + lab, 2) if (acts or lab) else None
    db.fetchrow(f"""UPDATE {P}.de_request SET est_cost = %s, actual_cost = %s WHERE id::text = %s
                    AND (est_cost IS DISTINCT FROM %s OR actual_cost IS DISTINCT FROM %s) RETURNING id""", (est, actual, request_id, est, actual))


def _cost_open(request_id: str) -> dict:
    row = get_row(request_id)
    if row is None or row["status"] in ("DRAFT", "REJECTED", "DONE"):
        raise DeError("Biaya diubah selama pengajuan masih berjalan")
    return row


def _amount(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    v = float(v)
    if v < 0 or v > 1e12:
        raise DeError("Nominal tidak valid")
    return round(v, 2)


def save_cost_item(user: dict, request_id: str, item_id: Optional[int], body: dict) -> dict:
    _cost_open(request_id)
    kind = body.get("kind") or "MATERIAL"
    if kind not in COST_KINDS:
        raise DeError("Jenis biaya tidak dikenal")
    desc = " ".join((body.get("description") or "").split())[:200]
    if not desc:
        raise DeError("Deskripsi biaya wajib diisi")
    qty = float(body.get("qty") or 1)
    if qty <= 0 or qty > 1e6:
        raise DeError("Qty tidak valid")
    est, act = _amount(body.get("estAmount")), _amount(body.get("actualAmount"))
    by = user.get("full_name") or user["username"]
    with db.transaction() as conn:
        if item_id is None:
            conn.execute(f"""INSERT INTO {P}.de_cost_item (request_id, kind, description, qty, est_amount, actual_amount, created_by)
                             VALUES (%s, %s, %s, %s, %s, %s, %s)""", (request_id, kind, desc, qty, est, act, by))
        else:
            n = conn.execute(f"""UPDATE {P}.de_cost_item SET kind = %s, description = %s, qty = %s, est_amount = %s, actual_amount = %s,
                                 created_by = %s, updated_at = now() WHERE id = %s AND request_id::text = %s""",
                             (kind, desc, qty, est, act, by, item_id, request_id)).rowcount
            if not n:
                raise LookupError("Baris biaya tidak ditemukan")
        _event(conn, request_id, user, "COST", None, None, None,
               {"item": desc, "estAmount": est, "actualAmount": act, "action": "add" if item_id is None else "edit"})
    recompute(request_id)
    return team_full(request_id)


def delete_cost_item(user: dict, request_id: str, item_id: int) -> dict:
    _cost_open(request_id)
    with db.transaction() as conn:
        row = conn.execute(f"DELETE FROM {P}.de_cost_item WHERE id = %s AND request_id::text = %s RETURNING description",
                           (item_id, request_id)).fetchone()
        if row:
            _event(conn, request_id, user, "COST", None, None, None, {"item": row["description"], "action": "delete"})
    recompute(request_id)
    return team_full(request_id)


def set_labor(user: dict, request_id: str, assignment_id: int, adjust: Optional[float], note: Optional[str]) -> dict:
    _cost_open(request_id)
    adjust = _amount(adjust)
    note = (note or "").strip()[:300] or None
    if adjust is not None and not note:
        raise DeError("Alasan penyesuaian upah wajib diisi")
    with db.transaction() as conn:
        row = conn.execute(f"""UPDATE {P}.de_assignment a SET labor_adjust = %s, labor_note = %s FROM {P}.de_technician t
                               WHERE a.id = %s AND a.request_id::text = %s AND t.id = a.technician_id RETURNING t.name""",
                           (adjust, note if adjust is not None else None, assignment_id, request_id)).fetchone()
        if row is None:
            raise LookupError("Penugasan tidak ditemukan")
        _event(conn, request_id, user, "COST", None, None, note, {"labor": row["name"], "adjust": adjust})
    recompute(request_id)
    return team_full(request_id)
