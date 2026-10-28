"""Form → Pengajuan and Monitoring → Pengajuan (2026-10-28, migration 034).

An outlet account reports the petty cash of its branch for a period and asks for the money spent: the baristas on duty
(accounts of the branch and names typed), the opening balance (the closing balance of the branch's previous pengajuan is
suggested; a different figure needs a reason), the money in (with its transfer proof), every expense (date, category,
description, amount, at least one receipt: nota, kwitansi, invoice …) and the closing balance (opening + in − out).
A draft is saved as it is filled; sending gives it a number (PGJ-YYMM-NNNN).

FLOW: SUBMITTED → VERIFIED (Operations checks the evidence, page mon.pengajuan.verify) → APPROVED (Finance accepts the
amount: the expenses not rejected, page mon.pengajuan.finance) → PAID (Finance records the transfer with its proof).
Each step may send it back as REVISION (the outlet corrects and sends again) or REJECTED; both need a note. A checker may
reject single expenses (with a note): they are left out of the approved amount. Every step is an event (the history the
outlet and the team follow). A receipt already used in another pengajuan (same file) is flagged for the team.
Files on the backend volume (MEDIA_DIR/pengajuan).
"""
import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db
from app.config import get_settings
from app.pic_checkin import CheckInError, branch_options

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
MAX_LINES = 100
MAX_FILES_PER_LINE = 5
MAX_OTHER_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_AMOUNT = Decimal("1000000000000")
MAX_BARISTAS = 20
CATEGORIES = {
    "RAW": "Bahan baku", "OPERATIONAL": "Operasional outlet", "GAS_WATER": "Gas & air galon", "TRANSPORT": "Transportasi & kirim",
    "CLEANING": "Kebersihan & ATK", "REPAIR": "Perbaikan ringan", "PARKING": "Parkir & keamanan", "MEAL": "Konsumsi", "OTHER": "Lainnya",
}
DOC_TYPES = {"NOTA": "Nota", "KWITANSI": "Kwitansi", "INVOICE": "Invoice", "STRUK": "Struk", "TRANSFER": "Bukti transfer",
             "FOTO": "Foto barang", "OTHER": "Lainnya"}
STATUS_LABEL = {"DRAFT": "Draf", "SUBMITTED": "Menunggu verifikasi", "REVISION": "Revisi", "VERIFIED": "Menunggu approval",
                "APPROVED": "Menunggu pembayaran", "PAID": "Dibayar", "REJECTED": "Ditolak"}
EDITABLE = ("DRAFT", "REVISION")
OPEN = ("SUBMITTED", "REVISION", "VERIFIED", "APPROVED")
# status → {next status: the permission it needs}
FLOW = {
    "SUBMITTED": {"VERIFIED": "verify", "REVISION": "verify", "REJECTED": "verify"},
    "VERIFIED": {"APPROVED": "finance", "REVISION": "finance", "REJECTED": "finance"},
    "APPROVED": {"PAID": "finance", "REVISION": "finance"},
}
NOTE_REQUIRED = ("REVISION", "REJECTED")
# the step whose checker reviews single expenses
LINE_REVIEW = {"SUBMITTED": "verify", "VERIFIED": "finance"}


class PengajuanError(CheckInError):
    """Refused with a message for the user (HTTP 422)."""


def _now() -> datetime:
    return datetime.now(WIB)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _num(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _actor(user: Optional[dict]) -> Optional[str]:
    return (user.get("full_name") or user["username"]) if user else None


def _amount(v: Any, what: str, allow_negative: bool = False) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v)).quantize(Decimal("1"))
    except Exception:
        raise PengajuanError(f"{what} harus angka rupiah") from None
    if (d < 0 and not allow_negative) or abs(d) > MAX_AMOUNT:
        raise PengajuanError(f"{what} harus angka rupiah 0 sampai 1 triliun")
    return d


def _date(v: Any) -> Optional[date]:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise PengajuanError("Tanggal tidak valid") from None


# ---------------------------------------------------------------- files (backend volume)

def _root() -> Path:
    root = Path(get_settings().media_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _file_type(data: bytes) -> tuple[str, str]:
    """A receipt, an invoice or a transfer proof: a PDF or a photo; anything else is refused."""
    if data[:5] == b"%PDF-":
        return "application/pdf", "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        raise PengajuanError("Foto HEIC belum didukung: ubah kamera ke JPG atau unggah tangkapan layar / PDF")
    raise PengajuanError("Lampiran harus PDF atau foto (JPG, PNG, WebP)")


def files_of(pengajuan_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT f.id::text, f.line_id, f.kind, f.doc_type, f.mime, f.bytes, f.file_name, f.uploaded_at, f.uploaded_by_name,
                               (SELECT array_agg(DISTINCT p.number) FROM {P}.pengajuan_file o JOIN {P}.pengajuan p ON p.id = o.pengajuan_id
                                 WHERE o.sha256 = f.sha256 AND o.pengajuan_id <> f.pengajuan_id AND p.status <> 'DRAFT') AS used_in
                        FROM {P}.pengajuan_file f WHERE f.pengajuan_id::text = %s ORDER BY f.uploaded_at""", (pengajuan_id,))
    return [{"id": r["id"], "lineId": r["line_id"], "kind": r["kind"], "docType": r["doc_type"], "mime": r["mime"], "bytes": r["bytes"],
             "fileName": r["file_name"], "uploadedAt": _iso(r["uploaded_at"]), "uploadedBy": r["uploaded_by_name"],
             "usedIn": [n for n in (r["used_in"] or []) if n]} for r in rows]


def add_file(user: dict, pengajuan_id: str, kind: str, data: bytes, file_name: str, doc_type: str, line_id: Optional[int]) -> dict:
    """kind LINE (a receipt of one expense), INCOME (the proof of the money in), OTHER (anything else), PAYMENT (Finance)."""
    if kind not in ("LINE", "INCOME", "OTHER", "PAYMENT"):
        raise PengajuanError("Jenis lampiran tidak dikenal")
    if doc_type not in DOC_TYPES:
        raise PengajuanError("Jenis dokumen tidak dikenal")
    if not data:
        raise PengajuanError("File kosong")
    if len(data) > MAX_FILE_BYTES:
        raise PengajuanError(f"Lampiran maksimal {MAX_FILE_BYTES // 1048576} MB")
    if kind == "LINE":
        if line_id is None or not db.fetchrow(f"SELECT 1 AS ok FROM {P}.pengajuan_line WHERE id = %s AND pengajuan_id::text = %s",
                                              (line_id, pengajuan_id)):
            raise PengajuanError("Simpan pengeluaran ini dulu, lalu lampirkan buktinya")
        n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.pengajuan_file WHERE line_id = %s", (line_id,))["n"]
        if n >= MAX_FILES_PER_LINE:
            raise PengajuanError(f"Maksimal {MAX_FILES_PER_LINE} lampiran per pengeluaran; hapus satu dulu")
    else:
        line_id = None
        n = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.pengajuan_file WHERE pengajuan_id::text = %s AND kind = %s",
                        (pengajuan_id, kind))["n"]
        if n >= MAX_OTHER_FILES:
            raise PengajuanError(f"Maksimal {MAX_OTHER_FILES} lampiran; hapus satu dulu")
    mime, ext = _file_type(data)
    file_id = str(uuid.uuid4())
    rel = f"pengajuan/{_now():%Y/%m}/{file_id}.{ext}"
    path = _root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    db.fetchrow(f"""INSERT INTO {P}.pengajuan_file (id, pengajuan_id, line_id, kind, doc_type, storage_path, mime, bytes, sha256, file_name,
                    uploaded_by, uploaded_by_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (file_id, pengajuan_id, line_id, kind, doc_type, rel, mime, len(data), hashlib.sha256(data).hexdigest(),
                 (file_name or "")[:160] or None, user["id"], _actor(user)))
    db.fetchrow(f"UPDATE {P}.pengajuan SET updated_at = now() WHERE id::text = %s RETURNING id", (pengajuan_id,))
    return {"files": files_of(pengajuan_id)}


def delete_file(pengajuan_id: str, file_id: str, kinds: tuple[str, ...]) -> dict:
    row = db.fetchrow(f"""DELETE FROM {P}.pengajuan_file WHERE id::text = %s AND pengajuan_id::text = %s AND kind = ANY(%s)
                          RETURNING storage_path""", (file_id, pengajuan_id, list(kinds)))
    if row:
        (_root() / row["storage_path"]).unlink(missing_ok=True)
    return {"files": files_of(pengajuan_id)}


def file_row(file_id: str) -> Optional[dict]:
    row = db.fetchrow(f"""SELECT f.storage_path, f.mime, f.file_name, f.kind, f.pengajuan_id::text FROM {P}.pengajuan_file f
                          WHERE f.id::text = %s""", (file_id,))
    if row:
        path = _root() / row["storage_path"]
        row["path"] = path if path.is_file() else None
    return row


# ---------------------------------------------------------------- reading

COLS = f"""p.id::text, p.number, p.status, p.user_id::text, p.username, p.full_name, p.branch_code, p.branch_name, p.baristas,
    p.period_from, p.period_to, p.opening_balance, p.opening_expected, p.opening_note, p.income_amount, p.income_note, p.total_out,
    p.closing_balance, p.note, p.approved_amount, p.paid_amount, p.paid_on, p.paid_note, p.created_at, p.updated_at, p.submitted_at,
    p.first_submitted_at, p.verified_at, p.verified_by, p.approved_at, p.approved_by, p.paid_at, p.paid_by, p.closed_at"""


def get_row(pengajuan_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {COLS} FROM {P}.pengajuan p WHERE p.id::text = %s", (pengajuan_id,))


def lines_of(pengajuan_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id, sort, spent_on, category, description, amount, review, review_note, reviewed_by
                        FROM {P}.pengajuan_line WHERE pengajuan_id::text = %s ORDER BY sort, id""", (pengajuan_id,))
    return [{"id": r["id"], "spentOn": _iso(r["spent_on"]), "category": r["category"], "categoryLabel": CATEGORIES.get(r["category"], r["category"]),
             "description": r["description"], "amount": _num(r["amount"]), "review": r["review"], "reviewNote": r["review_note"],
             "reviewedBy": r["reviewed_by"]} for r in rows]


def events_of(pengajuan_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT at, actor_name, kind, from_status, to_status, note, details FROM {P}.pengajuan_event
                        WHERE pengajuan_id::text = %s ORDER BY at, id""", (pengajuan_id,))
    return [{"at": _iso(r["at"]), "actor": r["actor_name"], "kind": r["kind"], "from": r["from_status"], "to": r["to_status"],
             "note": r["note"], "details": r["details"] or {}} for r in rows]


def view(row: dict, full: bool = False) -> dict:
    out = {
        "id": row["id"], "number": row["number"], "status": row["status"], "statusLabel": STATUS_LABEL[row["status"]],
        "userId": row["user_id"], "username": row["username"], "name": row["full_name"] or row["username"],
        "branchCode": row["branch_code"], "branchName": row["branch_name"], "baristas": row["baristas"] or [],
        "periodFrom": _iso(row["period_from"]), "periodTo": _iso(row["period_to"]),
        "openingBalance": _num(row["opening_balance"]), "openingExpected": _num(row["opening_expected"]), "openingNote": row["opening_note"],
        "incomeAmount": _num(row["income_amount"]), "incomeNote": row["income_note"], "totalOut": _num(row["total_out"]),
        "closingBalance": _num(row["closing_balance"]), "note": row["note"], "approvedAmount": _num(row["approved_amount"]),
        "paidAmount": _num(row["paid_amount"]), "paidOn": _iso(row["paid_on"]), "paidNote": row["paid_note"],
        "createdAt": _iso(row["created_at"]), "updatedAt": _iso(row["updated_at"]), "submittedAt": _iso(row["submitted_at"]),
        "firstSubmittedAt": _iso(row["first_submitted_at"]), "verifiedAt": _iso(row["verified_at"]), "verifiedBy": row["verified_by"],
        "approvedAt": _iso(row["approved_at"]), "approvedBy": row["approved_by"], "paidAt": _iso(row["paid_at"]), "paidBy": row["paid_by"],
        "closedAt": _iso(row["closed_at"]),
    }
    if full:
        out["lines"] = lines_of(row["id"])
        out["files"] = files_of(row["id"])
        out["events"] = events_of(row["id"])
        out["missing"] = missing(row, out["lines"], out["files"]) if row["status"] in EDITABLE else []
    return out


# ---------------------------------------------------------------- outlet (form.pengajuan)

def barista_options(branch_code: str) -> list[dict]:
    """Active personal accounts of the branch (not the shared outlet / hub accounts)."""
    rows = db.fetch(f"""SELECT u.id::text, COALESCE(NULLIF(u.full_name, ''), u.username) AS name FROM {P}.user_account u
                        JOIN {P}.user_branch b ON b.user_id = u.id AND b.branch_code = %s
                        WHERE u.is_active AND u.role = 'user'
                          AND split_part(u.username, '.', 1) NOT IN ('outlet', 'hubck', 'hubwh', 'wasgee', 'wh', 'ck', 'roastery')
                        ORDER BY 2""", (branch_code,))
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def form_state(user: dict) -> dict:
    branches = [{"code": b["code"], "name": b["name"]} for b in branch_options(user)]
    rows = db.fetch(f"""SELECT {COLS} FROM {P}.pengajuan p WHERE p.user_id::text = %s
                        ORDER BY (p.status IN ('DRAFT', 'REVISION')) DESC, COALESCE(p.submitted_at, p.updated_at) DESC LIMIT 60""",
                    (str(user["id"]),))
    return {"user": {"id": str(user["id"]), "username": user["username"], "name": user.get("full_name") or user["username"]},
            "branches": branches, "requests": [view(r) for r in rows], "categories": CATEGORIES, "docTypes": DOC_TYPES,
            "statusLabel": STATUS_LABEL, "baristas": {b["code"]: barista_options(b["code"]) for b in branches}}


def previous_closing(branch_code: str, exclude: Optional[str] = None) -> Optional[Decimal]:
    """Closing balance of the branch's latest sent pengajuan (not rejected): the next opening balance."""
    r = db.fetchrow(f"""SELECT closing_balance FROM {P}.pengajuan WHERE branch_code = %s AND status NOT IN ('DRAFT', 'REJECTED')
                          AND (%s::text IS NULL OR id::text <> %s) AND closing_balance IS NOT NULL
                        ORDER BY period_to DESC NULLS LAST, first_submitted_at DESC LIMIT 1""", (branch_code, exclude, exclude))
    return r["closing_balance"] if r else None


def create_draft(user: dict, branch_code: str) -> dict:
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise PengajuanError("Cabang tidak tersedia untuk akun Anda")
    row = db.fetchrow(f"""SELECT id::text FROM {P}.pengajuan WHERE user_id::text = %s AND branch_code = %s AND status = 'DRAFT'
                          ORDER BY updated_at DESC LIMIT 1""", (str(user["id"]), branch_code))
    if row is None:
        prev = previous_closing(branch_code)
        me = {"name": user.get("full_name") or user["username"]}
        row = db.fetchrow(f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, branch_code, branch_name, baristas,
                                  opening_balance, opening_expected, closing_balance)
                              VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING id::text""",
                          (user["id"], user["username"], user.get("full_name"), branch["code"], branch["name"],
                           json.dumps([] if user["username"].split(".")[0] in ("outlet", "wasgee") else [me]), prev, prev, prev))
    return full(row["id"])


def full(pengajuan_id: str) -> dict:
    return view(get_row(pengajuan_id), full=True)


def own(user: dict, pengajuan_id: str, editable: bool = False) -> dict:
    row = get_row(pengajuan_id)
    if row is None or row["user_id"] != str(user["id"]):
        raise LookupError("Pengajuan tidak ditemukan")
    if editable and row["status"] not in EDITABLE:
        raise PengajuanError("Pengajuan ini sudah dikirim; menunggu proses tim")
    return row


def _recompute(conn, pengajuan_id: str) -> None:
    conn.execute(f"""UPDATE {P}.pengajuan p SET
                        total_out = COALESCE((SELECT sum(amount) FROM {P}.pengajuan_line l WHERE l.pengajuan_id = p.id), 0),
                        closing_balance = CASE WHEN p.opening_balance IS NULL THEN NULL ELSE p.opening_balance + p.income_amount
                            - COALESCE((SELECT sum(amount) FROM {P}.pengajuan_line l WHERE l.pengajuan_id = p.id), 0) END,
                        updated_at = now()
                     WHERE p.id::text = %s""", (pengajuan_id,))


def save_draft(user: dict, pengajuan_id: str, body: dict) -> dict:
    """Merges the fields given; `lines` is the whole list (an id keeps a line and its receipts, a line left out is removed)."""
    own(user, pengajuan_id, editable=True)
    sets: dict[str, Any] = {}
    if "baristas" in body:
        names = []
        for b in body["baristas"] or []:
            name = " ".join(str((b or {}).get("name") or "").split())[:80]
            if name and name.lower() not in {n["name"].lower() for n in names}:
                names.append({"name": name, **({"userId": str(b["userId"])} if b.get("userId") else {})})
        if len(names) > MAX_BARISTAS:
            raise PengajuanError(f"Maksimal {MAX_BARISTAS} barista")
        sets["baristas"] = json.dumps(names)
    for key, col in (("periodFrom", "period_from"), ("periodTo", "period_to")):
        if key in body:
            sets[col] = _date(body[key])
    if "openingBalance" in body:
        sets["opening_balance"] = _amount(body["openingBalance"], "Saldo awal", allow_negative=True)
    if "incomeAmount" in body:
        sets["income_amount"] = _amount(body["incomeAmount"], "Dana masuk") or Decimal(0)
    for key, col in (("openingNote", "opening_note"), ("incomeNote", "income_note"), ("note", "note")):
        if key in body:
            sets[col] = (str(body[key] or "")).strip()[:1000] or None
    lines = body.get("lines")
    if lines is not None and len(lines) > MAX_LINES:
        raise PengajuanError(f"Maksimal {MAX_LINES} pengeluaran per pengajuan")
    clean = []
    for i, ln in enumerate(lines or [], start=1):
        cat = ln.get("category") or "OTHER"
        if cat not in CATEGORIES:
            raise PengajuanError("Kategori pengeluaran tidak dikenal")
        clean.append({"id": ln.get("id"), "sort": i, "spent_on": _date(ln.get("spentOn")),
                      "category": cat, "description": " ".join(str(ln.get("description") or "").split())[:300],
                      "amount": _amount(ln.get("amount"), f"Nominal pengeluaran {i}") or Decimal(0)})
    with db.transaction() as conn:
        if sets:
            cols = ", ".join(f"{c} = %s" + ("::jsonb" if c == "baristas" else "") for c in sets)
            conn.execute(f"UPDATE {P}.pengajuan SET {cols} WHERE id::text = %s", (*sets.values(), pengajuan_id))
        if lines is not None:
            keep = [ln["id"] for ln in clean if ln["id"]]
            conn.execute(f"DELETE FROM {P}.pengajuan_line WHERE pengajuan_id::text = %s AND NOT (id = ANY(%s::int[]))", (pengajuan_id, keep))
            for ln in clean:
                vals = (ln["sort"], ln["spent_on"], ln["category"], ln["description"], ln["amount"])
                hit = conn.execute(f"""UPDATE {P}.pengajuan_line SET sort = %s, spent_on = %s, category = %s, description = %s, amount = %s
                                       WHERE id = %s AND pengajuan_id::text = %s RETURNING id""", (*vals, ln["id"], pengajuan_id)).fetchone() if ln["id"] else None
                if hit is None:
                    conn.execute(f"""INSERT INTO {P}.pengajuan_line (pengajuan_id, sort, spent_on, category, description, amount)
                                     VALUES (%s, %s, %s, %s, %s, %s)""", (pengajuan_id, *vals))
        _recompute(conn, pengajuan_id)
    return full(pengajuan_id)


def missing(row: dict, lines: list[dict], files: list[dict]) -> list[str]:
    """What blocks the sending (the same list shows in the form)."""
    out = []
    if not row["baristas"]:
        out.append("Isi minimal satu nama barista")
    pf, pt = row["period_from"], row["period_to"]
    if not pf or not pt:
        out.append("Isi periode pengajuan (dari – sampai)")
    elif pf > pt:
        out.append("Tanggal mulai periode harus sebelum tanggal akhir")
    elif pt > _now().date():
        out.append("Periode tidak boleh melewati hari ini")
    if row["opening_balance"] is None:
        out.append("Isi saldo awal")
    elif row["opening_expected"] is not None and row["opening_balance"] != row["opening_expected"] and not (row["opening_note"] or "").strip():
        out.append("Saldo awal berbeda dari saldo akhir pengajuan sebelumnya: tulis alasannya")
    if row["income_amount"] and row["income_amount"] > 0:
        if not (row["income_note"] or "").strip():
            out.append("Tulis sumber dana masuk (mis. transfer Finance tanggal …)")
        if not any(f["kind"] == "INCOME" for f in files):
            out.append("Lampirkan bukti dana masuk (bukti transfer / setoran)")
    if not lines:
        out.append("Tambahkan minimal satu pengeluaran")
    with_file = {f["lineId"] for f in files if f["kind"] == "LINE"}
    for i, ln in enumerate(lines, start=1):
        problems = []
        if not ln["spentOn"]:
            problems.append("tanggal")
        elif pf and pt and not (pf.isoformat() <= ln["spentOn"] <= pt.isoformat()):
            problems.append("tanggal di luar periode")
        if not ln["description"]:
            problems.append("keterangan")
        if not ln["amount"] or ln["amount"] <= 0:
            problems.append("nominal")
        if ln["id"] not in with_file:
            problems.append("nota / kwitansi / invoice")
        if problems:
            out.append(f"Pengeluaran {i}: {', '.join(problems)}")
    if row["closing_balance"] is not None and row["closing_balance"] < 0 and not (row["note"] or "").strip():
        out.append("Saldo akhir minus: tulis penjelasan di catatan")
    return out


def _next_number(conn) -> str:
    month = _now().strftime("%y%m")
    n = conn.execute(f"""INSERT INTO {P}.pengajuan_counter (month, n) VALUES (%s, 1)
                         ON CONFLICT (month) DO UPDATE SET n = pengajuan_counter.n + 1 RETURNING n""", (month,)).fetchone()["n"]
    return f"PGJ-{month}-{n:04d}"


def _event(conn, pengajuan_id: str, user: Optional[dict], kind: str, from_status: Optional[str] = None, to_status: Optional[str] = None,
           note: Optional[str] = None, details: Optional[dict] = None) -> None:
    conn.execute(f"""INSERT INTO {P}.pengajuan_event (pengajuan_id, actor_id, actor_name, kind, from_status, to_status, note, details)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                 (pengajuan_id, user["id"] if user else None, _actor(user), kind, from_status, to_status, note,
                  json.dumps(details or {}, ensure_ascii=False, default=str)))


def submit(user: dict, pengajuan_id: str) -> dict:
    row = own(user, pengajuan_id, editable=True)
    lines, files = lines_of(pengajuan_id), files_of(pengajuan_id)
    todo = missing(row, lines, files)
    if todo:
        raise PengajuanError("Belum bisa dikirim: " + "; ".join(todo))
    with db.transaction() as conn:
        number = row["number"] or _next_number(conn)
        # a revision is checked again from the start
        conn.execute(f"UPDATE {P}.pengajuan_line SET review = 'OK', review_note = NULL, reviewed_by = NULL WHERE pengajuan_id::text = %s",
                     (pengajuan_id,))
        conn.execute(f"""UPDATE {P}.pengajuan SET number = %s, status = 'SUBMITTED', submitted_at = now(),
                             first_submitted_at = COALESCE(first_submitted_at, now()), verified_at = NULL, verified_by = NULL,
                             approved_at = NULL, approved_by = NULL, approved_amount = NULL, updated_at = now() WHERE id::text = %s""",
                     (number, pengajuan_id))
        _event(conn, pengajuan_id, user, "SUBMIT", row["status"], "SUBMITTED", None,
               {"totalOut": row["total_out"], "lines": len(lines), "files": len(files), "resubmit": row["status"] == "REVISION"})
    return full(pengajuan_id)


def discard(user: dict, pengajuan_id: str) -> None:
    row = own(user, pengajuan_id)
    if row["status"] != "DRAFT":
        raise PengajuanError("Hanya draf yang dapat dihapus")
    paths = [r["storage_path"] for r in db.fetch(f"SELECT storage_path FROM {P}.pengajuan_file WHERE pengajuan_id::text = %s", (pengajuan_id,))]
    db.fetchrow(f"DELETE FROM {P}.pengajuan WHERE id::text = %s AND status = 'DRAFT' RETURNING id", (pengajuan_id,))
    for p in paths:
        (_root() / p).unlink(missing_ok=True)


# ---------------------------------------------------------------- team (mon.pengajuan)

def allowed_next(row: dict, perms: set[str]) -> list[str]:
    return [to for to, need in FLOW.get(row["status"], {}).items() if need in perms]


def change_status(user: dict, pengajuan_id: str, to: str, note: Optional[str], perms: set[str], paid: Optional[dict] = None) -> dict:
    row = get_row(pengajuan_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    need = FLOW.get(row["status"], {}).get(to)
    if need is None:
        raise PengajuanError(f"Tidak dapat mengubah status {STATUS_LABEL[row['status']]} ke {STATUS_LABEL.get(to, to)}")
    if need not in perms:
        raise PermissionError("Langkah ini bukan bagian akses Anda")
    note = (note or "").strip()[:2000] or None
    if to in NOTE_REQUIRED and not note:
        raise PengajuanError("Tulis alasannya agar outlet tahu apa yang harus diperbaiki" if to == "REVISION" else "Tulis alasan penolakan")
    actor = _actor(user)
    sets, details = [], {}
    if to == "VERIFIED":
        sets += ["verified_at = now()", "verified_by = %(actor)s"]
    if to == "APPROVED":
        ok = sum((Decimal(str(ln["amount"] or 0)) for ln in lines_of(pengajuan_id) if ln["review"] == "OK"), Decimal(0))
        details["approvedAmount"] = ok
        sets += ["approved_at = now()", "approved_by = %(actor)s", "approved_amount = %(approved)s"]
    if to == "PAID":
        paid = paid or {}
        amount = _amount(paid.get("amount"), "Nominal pembayaran")
        if amount is None or amount <= 0:
            raise PengajuanError("Isi nominal yang ditransfer")
        on = _date(paid.get("on"))
        if on is None:
            raise PengajuanError("Isi tanggal transfer")
        if not any(f["kind"] == "PAYMENT" for f in files_of(pengajuan_id)):
            raise PengajuanError("Lampirkan bukti transfer dulu")
        details.update(amount=amount, on=on)
        sets += ["paid_at = now()", "paid_by = %(actor)s", "paid_amount = %(paid)s", "paid_on = %(paid_on)s", "paid_note = %(note)s",
                 "closed_at = now()"]
    if to == "REJECTED":
        sets.append("closed_at = now()")
    params = {"id": pengajuan_id, "to": to, "actor": actor, "approved": details.get("approvedAmount"), "paid": details.get("amount"),
              "paid_on": details.get("on"), "note": note}
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.pengajuan SET status = %(to)s, updated_at = now(){''.join(', ' + s for s in sets)} WHERE id::text = %(id)s",
                     params)
        _event(conn, pengajuan_id, user, "STATUS", row["status"], to, note, details)
    return full(pengajuan_id)


def review_line(user: dict, pengajuan_id: str, line_id: int, review: str, note: Optional[str], perms: set[str]) -> dict:
    row = get_row(pengajuan_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    need = LINE_REVIEW.get(row["status"])
    if need is None:
        raise PengajuanError("Pengeluaran diperiksa saat verifikasi atau approval")
    if need not in perms:
        raise PermissionError("Pemeriksaan di langkah ini bukan bagian akses Anda")
    if review not in ("OK", "REJECTED"):
        raise PengajuanError("Hasil pemeriksaan harus OK atau REJECTED")
    note = (note or "").strip()[:500] or None
    if review == "REJECTED" and not note:
        raise PengajuanError("Tulis alasan pengeluaran ini ditolak")
    with db.transaction() as conn:
        hit = conn.execute(f"""UPDATE {P}.pengajuan_line SET review = %s, review_note = %s, reviewed_by = %s
                               WHERE id = %s AND pengajuan_id::text = %s RETURNING description, amount""",
                           (review, note if review == "REJECTED" else None, _actor(user), line_id, pengajuan_id)).fetchone()
        if hit is None:
            raise LookupError("Pengeluaran tidak ditemukan")
        _event(conn, pengajuan_id, user, "LINE", None, None, note,
               {"line": line_id, "review": review, "description": hit["description"], "amount": hit["amount"]})
    return full(pengajuan_id)


def add_note(user: dict, pengajuan_id: str, note: str) -> dict:
    with db.transaction() as conn:
        _event(conn, pengajuan_id, user, "NOTE", None, None, note.strip()[:2000])
    return full(pengajuan_id)


def monitor(date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    """Sent pengajuan of the period (first sending, WIB days) and every one still open, with their expenses."""
    where = ["p.status <> 'DRAFT'",
             "((p.first_submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s OR p.status = ANY(%(open)s))"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "open": list(OPEN)}
    if branches is not None:
        where.append("p.branch_code = ANY(%(br)s)")
        params["br"] = branches
    rows = db.fetch(f"SELECT {COLS} FROM {P}.pengajuan p WHERE {' AND '.join(where)} ORDER BY p.submitted_at DESC LIMIT 2000", params)
    ids = [r["id"] for r in rows]
    lines: dict[str, list] = {}
    stats: dict[str, dict] = {}
    if ids:
        for ln in db.fetch(f"""SELECT pengajuan_id::text AS pid, id, spent_on, category, description, amount, review, review_note
                               FROM {P}.pengajuan_line WHERE pengajuan_id::text = ANY(%s) ORDER BY sort, id""", (ids,)):
            lines.setdefault(ln["pid"], []).append({"id": ln["id"], "spentOn": _iso(ln["spent_on"]), "category": ln["category"],
                                                    "description": ln["description"], "amount": _num(ln["amount"]), "review": ln["review"],
                                                    "reviewNote": ln["review_note"]})
        for s in db.fetch(f"""SELECT f.pengajuan_id::text AS pid, count(*)::int AS files,
                                     count(*) FILTER (WHERE EXISTS (SELECT 1 FROM {P}.pengajuan_file o JOIN {P}.pengajuan q ON q.id = o.pengajuan_id
                                         WHERE o.sha256 = f.sha256 AND o.pengajuan_id <> f.pengajuan_id AND q.status <> 'DRAFT'))::int AS reused
                              FROM {P}.pengajuan_file f WHERE f.pengajuan_id::text = ANY(%s) GROUP BY 1""", (ids,)):
            stats[s["pid"]] = s
    data = []
    for r in rows:
        v = view(r)
        v["lines"] = lines.get(r["id"], [])
        v["fileCount"] = stats.get(r["id"], {}).get("files", 0)
        v["reusedFiles"] = stats.get(r["id"], {}).get("reused", 0)
        data.append(v)
    return {"data": data, "categories": CATEGORIES, "docTypes": DOC_TYPES, "statusLabel": STATUS_LABEL}
