"""Form → Pengajuan (Form Fund) and Monitoring → Pengajuan (2026-10-28, migrations 034 and 037).

The outlet fills it in Operations, management approves it in the portal (MoM 2026-10-27: Finance reviews and reconciles,
it does not type anything again). Every kind asks who submits it (barista / PIC, personal phone) and the bank account the
money goes to (the last one of the branch is suggested).
  * PETTY_CASH, weekly and request based (no request, no transfer): the baristas on duty, the period (one per branch per
    period: periods may not overlap), the opening balance (the closing balance of the branch's previous petty cash is
    suggested; a different figure needs a reason), the money in (with its proof), every expense with its receipt and
    the closing balance (opening + in − out). It asks for the top-up up to the plafon (the outlet's own, else the one
    of every outlet; no plafon: the money spent).
  * budget requests EMERGENCY (already paid, receipts required), MARKETING, EVENT, BILL (invoice required): the purpose,
    the date needed (event date, due date …) and the lines (estimated cost; a receipt where required).
A draft is saved as it is filled; sending gives it a number (PGJ-YYMM-NNNN).

FLOW (set per kind by management, approval_step): SUBMITTED waits at approval step 1, 2 … (each step: its users;
without users only superadmins) → APPROVED (the amount: the lines not rejected) → PAID (the payment step records the
transfer with its proof). Every step may send it back as REVISION (the outlet corrects and sends again, the approvals
start over) or REJECTED; both need a note. The approver of the step may reject single lines (with a note): they are left
out of the approved amount. Every step is an event (the history the outlet and the team follow). A receipt already used
in another pengajuan (same file) is flagged. Files on the backend volume (MEDIA_DIR/pengajuan).
"""
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db
from app.config import get_settings
from app.pic_checkin import CheckInError, branch_options
from app.utils import today as today_wib

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
MAX_LINES = 100
MAX_FILES_PER_LINE = 5
MAX_OTHER_FILES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_AMOUNT = Decimal("1000000000000")
MAX_BARISTAS = 20
KINDS = {"PETTY_CASH": "Petty Cash Mingguan", "EMERGENCY": "Pembelian Darurat", "MARKETING": "Marketing & Promo",
         "EVENT": "Event & Bazar", "BILL": "Tagihan Outlet", "PERSONAL": "Pengajuan Personal",
         "MAINTENANCE": "Maintenance", "PROCUREMENT": "Procurement", "VENDOR": "Pembayaran Vendor", "INTERCOMPANY": "Antar-PT"}
TERMS = ("COD", "TOP", "CBD")
ONE_AMOUNT = ("PERSONAL", "VENDOR", "INTERCOMPANY")  # no expense lines: one amount
# asked by the Maintenance & Procurement team from a Damaged Equipment request, never from the outlet form (app/de_funds.py)
DE_KINDS = ("MAINTENANCE", "PROCUREMENT")
OUTLET_KINDS = ("PETTY_CASH", "EMERGENCY", "MARKETING", "EVENT", "BILL")
CATEGORIES_OF = {
    "PETTY_CASH": {"RAW": "Bahan baku", "OPERATIONAL": "Operasional outlet", "GAS_WATER": "Gas & air galon",
                   "TRANSPORT": "Transportasi & kirim", "CLEANING": "Kebersihan & ATK", "REPAIR": "Perbaikan ringan",
                   "PARKING": "Parkir & keamanan", "MEAL": "Konsumsi", "OTHER": "Lainnya"},
    "EMERGENCY": {"RAW": "Bahan baku", "PACKAGING": "Kemasan & cup", "ICE": "Es batu", "GAS_WATER": "Gas & air galon",
                  "CLEANING": "Kebersihan & ATK", "OTHER": "Lainnya"},
    "MARKETING": {"PRINT": "Cetak (banner, flyer, menu)", "VOUCHER": "Voucher & diskon", "SAMPLING": "Sampling produk",
                  "ADS": "Iklan & endorse", "MERCH": "Merchandise", "OTHER": "Lainnya"},
    "EVENT": {"BOOTH": "Sewa booth / lokasi", "EQUIPMENT": "Sewa peralatan", "STOCK": "Bahan & produk", "TRANSPORT": "Transportasi & kirim",
              "CREW": "Konsumsi & crew", "DECOR": "Dekorasi", "OTHER": "Lainnya"},
    "BILL": {"ELECTRIC": "Listrik", "WATER": "Air (PDAM)", "INTERNET": "Internet & telepon", "WASTE": "Sampah",
             "SECURITY": "Keamanan & lingkungan", "RENT": "Sewa tempat", "OTHER": "Lainnya"},
}
CATEGORIES_OF["VENDOR"] = CATEGORIES_OF["INTERCOMPANY"] = {}
CATEGORIES_OF["PERSONAL"] = {}  # a personal request has one amount, no expense lines (2026-11-20)
DE_CATEGORIES = {"MATERIAL": "Material / barang", "SERVICE": "Jasa vendor", "TRANSPORT": "Transport", "OTHER": "Lainnya"}
CATEGORIES_OF.update({k: DE_CATEGORIES for k in DE_KINDS})
CATEGORIES = {k: v for cats in CATEGORIES_OF.values() for k, v in cats.items()}
RECEIPT_REQUIRED = ("PETTY_CASH", "EMERGENCY", "BILL")   # every line needs its receipt / invoice
LINE_DATE_REQUIRED = ("PETTY_CASH", "EMERGENCY")
NEEDED_ON = {"MARKETING": "Tanggal dibutuhkan", "EVENT": "Tanggal event", "BILL": "Jatuh tempo"}  # required for these
DOC_TYPES = {"NOTA": "Nota", "KWITANSI": "Kwitansi", "INVOICE": "Invoice", "STRUK": "Struk", "TRANSFER": "Bukti transfer",
             "FOTO": "Foto barang", "QUOTE": "Penawaran harga", "OTHER": "Lainnya"}
# 2026-11-24 (migration 144): the whole life — Pending → Approved → In Progress (money out, proof awaited) → Verifikasi → Done
STATUS_LABEL = {"DRAFT": "Draf", "SUBMITTED": "Pending", "REVISION": "Revisi", "APPROVED": "Menunggu Transfer",
                "PAID": "In Progress", "VERIFYING": "Verifikasi", "DONE": "Done", "REJECTED": "Ditolak",
                "CANCELLED": "Dibatalkan"}
EDITABLE = ("DRAFT", "REVISION")
OPEN = ("SUBMITTED", "REVISION", "APPROVED", "PAID", "VERIFYING")
ACTIONS = ("APPROVE", "REVISION", "REJECTED", "PAID")
NOTE_REQUIRED = ("REVISION", "REJECTED", "CANCELLED")
CANCELLABLE = ("SUBMITTED", "REVISION", "APPROVED")  # before the money goes out
MAX_STEPS = 8


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
                                 WHERE o.sha256 = f.sha256 AND o.pengajuan_id <> f.pengajuan_id AND p.status <> 'DRAFT'
                                   AND f.kind <> 'PAYMENT') AS used_in  -- one bank batch proof on every request of it is meant
                        FROM {P}.pengajuan_file f WHERE f.pengajuan_id::text = %s ORDER BY f.uploaded_at""", (pengajuan_id,))
    return [{"id": r["id"], "lineId": r["line_id"], "kind": r["kind"], "docType": r["doc_type"], "mime": r["mime"], "bytes": r["bytes"],
             "fileName": r["file_name"], "uploadedAt": _iso(r["uploaded_at"]), "uploadedBy": r["uploaded_by_name"],
             "usedIn": [n for n in (r["used_in"] or []) if n]} for r in rows]


def add_file(user: dict, pengajuan_id: str, kind: str, data: bytes, file_name: str, doc_type: str, line_id: Optional[int]) -> dict:
    """kind LINE (a receipt of one expense), INCOME (the proof of the money in), OTHER (anything else), PAYMENT (Finance),
    REFUND (Finance: the money a Maintenance & Procurement fund gave back)."""
    if kind not in ("LINE", "INCOME", "OTHER", "PAYMENT", "REFUND", "PROOF", "TOPUP"):
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

COLS = """p.id::text, p.number, p.status, p.user_id::text, p.username, p.full_name, p.branch_code, p.branch_name, p.baristas,
    p.period_from, p.period_to, p.opening_balance, p.opening_expected, p.opening_note, p.income_amount, p.income_note, p.total_out,
    p.closing_balance, p.note, p.approved_amount, p.paid_amount, p.paid_on, p.paid_note, p.created_at, p.updated_at, p.submitted_at,
    p.first_submitted_at, p.approved_at, p.approved_by, p.paid_at, p.paid_by, p.closed_at, p.kind, p.approval_step,
    p.requester_name, p.requester_phone, p.payee_bank, p.payee_account, p.payee_name, p.purpose, p.needed_on, p.plafon,
    p.requested_amount, p.email, p.department, p.job_title, p.purpose_id, p.transaction_type_id, p.project_id,
    p.account_type_id, p.settles_id::text, p.de_request_id::text, p.refund_amount, p.refunded_at, p.refunded_by, p.refunded_note,
    p.proof_due_on, p.proof_submitted_at, p.actual_amount, p.actual_note, p.verified_amount, p.verified_note, p.verified_at, p.verified_by,
    p.done_at, p.topup_amount, p.topup_paid_at, p.payee_type, p.va_provider, p.invoice_no,
    p.unit, p.payer, p.vendor_name, p.payment_term, p.invoice_date, p.outstanding_amount,
    p.payee_pt, p.received_at, p.received_by, p.received_note"""


def get_row(pengajuan_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {COLS} FROM {P}.pengajuan p WHERE p.id::text = %s", (pengajuan_id,))


def category_label(code: Optional[str], names: Optional[dict[int, str]] = None) -> Optional[str]:
    """The label of a line category: a fixed one, or "TRX:<id>" = a Jenis Transaksi (Maintenance & Procurement, 142)."""
    if code and code.startswith("TRX:") and code[4:].isdigit():
        names = names if names is not None else _option_names()
        return names.get(int(code[4:]), code)
    return CATEGORIES.get(code, code) if code else code


def lines_of(pengajuan_id: str) -> list[dict]:
    rows = db.fetch(f"""SELECT id, sort, spent_on, category, description, amount, review, review_note, reviewed_by
                        FROM {P}.pengajuan_line WHERE pengajuan_id::text = %s ORDER BY sort, id""", (pengajuan_id,))
    names = _option_names() if any((r["category"] or "").startswith("TRX:") for r in rows) else {}
    return [{"id": r["id"], "spentOn": _iso(r["spent_on"]), "category": r["category"], "categoryLabel": category_label(r["category"], names),
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
        "firstSubmittedAt": _iso(row["first_submitted_at"]),
        "approvedAt": _iso(row["approved_at"]), "approvedBy": row["approved_by"], "paidAt": _iso(row["paid_at"]), "paidBy": row["paid_by"],
        "closedAt": _iso(row["closed_at"]), "kind": row["kind"], "kindLabel": kind_labels().get(row["kind"]) or KINDS[row["kind"]], "approvalStep": row["approval_step"],
        "requesterName": row["requester_name"], "requesterPhone": row["requester_phone"], "payeeBank": row["payee_bank"],
        "payeeAccount": row["payee_account"], "payeeName": row["payee_name"], "purpose": row["purpose"], "neededOn": _iso(row["needed_on"]),
        "plafon": _num(row["plafon"]), "requestedAmount": _num(row["requested_amount"]),
        "scope": "PERSONAL" if row["kind"] == "PERSONAL" else row["kind"] if row["kind"] in ("VENDOR", "INTERCOMPANY") else "OUTLET",
        "payeePt": row.get("payee_pt"), "receivedAt": _iso(row.get("received_at")), "receivedBy": row.get("received_by"), "receivedNote": row.get("received_note"),
        "unit": row.get("unit") or "CALF", "payer": row.get("payer"), "vendorName": row.get("vendor_name"), "paymentTerm": row.get("payment_term"),
        "invoiceDate": _iso(row.get("invoice_date")), "outstandingAmount": _num(row.get("outstanding_amount")), "email": row.get("email"), "department": row.get("department"),
        "jobTitle": row.get("job_title"), "purposeId": row.get("purpose_id"), "transactionTypeId": row.get("transaction_type_id"),
        "projectId": row.get("project_id"), "accountTypeId": row.get("account_type_id"), "settlesId": row.get("settles_id"),
        "refundAmount": _num(row.get("refund_amount")), "refundedAt": _iso(row.get("refunded_at")), "refundedBy": row.get("refunded_by"),
        "refundedNote": row.get("refunded_note"),
    }
    from app import fund_lifecycle
    out.update(fund_lifecycle.view_fields(row))
    if row.get("de_request_id"):  # asked by the Maintenance & Procurement team (app/de_funds.py)
        de = db.fetchrow(f"SELECT number, kind, vendor, full_name FROM {P}.de_request WHERE id::text = %s", (row["de_request_id"],))
        out["deRequest"] = {"id": row["de_request_id"], "number": de["number"], "kind": de["kind"], "vendor": de["vendor"],
                            "pic": de["full_name"]} if de else None
        out["scope"] = "DE"
    if row["kind"] == "PERSONAL":
        names = _option_names()
        out["kindLabel"] = names.get(row.get("purpose_id")) or KINDS["PERSONAL"]
        out["purposeName"] = names.get(row.get("purpose_id"))
        out["transactionTypeName"] = names.get(row.get("transaction_type_id"))
        out["projectName"] = names.get(row.get("project_id"))
        out["accountTypeName"] = names.get(row.get("account_type_id"))
        out["settles"] = settlement_of(row)
    if full:
        out["lines"] = lines_of(row["id"])
        out["files"] = files_of(row["id"])
        out["events"] = events_of(row["id"])
        out["similar"] = similar(row)
        out["missing"] = missing(row, out["lines"], out["files"]) if row["status"] in EDITABLE else []
        out["steps"] = [{"position": s["position"], "label": s["label"], "type": s["type"], "users": [u["name"] for u in s["users"]]}
                        for s in steps_of(row)]
        out["rule"] = fund_lifecycle.rule_for(row)
        out["proofMissing"] = fund_lifecycle.missing_proof(out["rule"], out["files"], row.get("actual_amount")) if row["status"] == "PAID" else []
        if row["kind"] == "PETTY_CASH" and row["status"] in EDITABLE:
            out["plafon"] = _num(plafon_of(row["branch_code"]))
        if row["kind"] == "PETTY_CASH":
            out["purchaseRef"] = purchase_reference(row["branch_code"], row["period_to"])
            out["purchaseDocs"] = purchase_docs(row["branch_code"], row["period_from"], row["period_to"])
    return out


# ---------------------------------------------------------------- approvals and plafon (set by management)

# the forms with approval steps: their kinds and the page the approvers get (Damaged Equipment: Review → Accept)
FLOW_FORMS = {"pengajuan": (KINDS, "mon.pengajuan"),
              "de": ({"REPAIR": "Maintenance", "PROCUREMENT": "Pengadaan (Dekorasi, Tools & Merchandise)"}, "mon.damaged-equipment")}


def flow(kind: str, form: str = "pengajuan") -> list[dict]:
    """The steps of one kind in order: the approval steps, then the payment step; each with its users."""
    steps = db.fetch(f"""SELECT s.id, s.position, s.label, s.step_type FROM {P}.approval_step s
                         WHERE s.form = %s AND s.kind = %s ORDER BY (s.step_type = 'PAY'), s.position""", (form, kind))
    users: dict[int, list] = {}
    if steps:
        for r in db.fetch(f"""SELECT su.step_id, u.id::text, COALESCE(NULLIF(u.full_name, ''), u.username) AS name, u.username
                              FROM {P}.approval_step_user su JOIN {P}.user_account u ON u.id = su.user_id
                              WHERE su.step_id = ANY(%s) AND u.is_active ORDER BY 3""", ([s["id"] for s in steps],)):
            users.setdefault(r["step_id"], []).append({"id": r["id"], "name": r["name"], "username": r["username"]})
    return [{"id": s["id"], "position": i, "label": s["label"], "type": s["step_type"], "users": users.get(s["id"], [])}
            for i, s in enumerate(steps, start=1)]


def flow_key(row: dict) -> str:
    return f"PERSONAL:{row['purpose_id']}" if row.get("kind") == "PERSONAL" and row.get("purpose_id") else row["kind"]


def flow_keys(row: dict) -> list[str]:
    """The flows a request may follow, the first that has steps counts: its unit's own (2026-11-24), then its kind's; a
    personal one its Keperluan Dana's, then the default personal flow. Pure (tested)."""
    unit = row.get("unit") or "CALF"
    keys = [flow_key(row)] + (["PERSONAL"] if row.get("kind") == "PERSONAL" and flow_key(row) != "PERSONAL" else [])
    return [k for key in keys for k in (f"{key}@{unit}", key)]


def steps_of(row: dict) -> list[dict]:
    steps: list[dict] = []
    for key in flow_keys(row):
        steps = flow(key)
        if steps:
            break
    return steps


def _approvals(steps: list[dict]) -> list[dict]:
    return [s for s in steps if s["type"] == "APPROVE"]


def _pay(steps: list[dict]) -> Optional[dict]:
    return next((s for s in steps if s["type"] == "PAY"), None)


def takes(user: dict, step: Optional[dict]) -> bool:
    """A step is taken by its users; a step without users by the superadmins only (a superadmin may always act)."""
    return step is not None and (user.get("role") == "superadmin" or any(u["id"] == str(user["id"]) for u in step["users"]))


def current_step(row: dict, steps: Optional[list[dict]] = None) -> Optional[dict]:
    steps = steps_of(row) if steps is None else steps
    if row["status"] == "SUBMITTED":
        appr = _approvals(steps)
        return appr[min(max(row["approval_step"], 1), len(appr)) - 1] if appr else None
    if row["status"] in ("APPROVED", "VERIFYING"):  # Finance: the payment, then the proof
        return _pay(steps)
    return None


def sees(user: dict, row: dict, steps: list[dict]) -> bool:
    """2026-11-27 (Finance): a request still in approval (Pending / Revisi) is shown only to the approvers it has reached
    (the step waiting and the ones before it); Finance and the other monitors see it once every approval is given. The
    superadmins see everything. Pure (tested)."""
    if user.get("role") == "superadmin" or row["status"] not in ("SUBMITTED", "REVISION"):
        return True
    me = str(user["id"])
    appr = _approvals(steps)
    reached = appr[:min(max(int(row.get("approval_step") or 1), 1), len(appr))] if appr else []
    return any(u["id"] == me for s in reached for u in s["users"])


def approver_of_any(user: dict) -> bool:
    return bool(db.fetchrow(f"""SELECT 1 AS ok FROM {P}.approval_step_user su JOIN {P}.approval_step s ON s.id = su.step_id
                                WHERE s.form = 'pengajuan' AND su.user_id::text = %s LIMIT 1""", (str(user["id"]),)))


def set_flow(user: dict, kind: str, steps: list[dict], form: str = "pengajuan") -> list[dict]:
    """Replaces the steps of a kind: [{label, userIds}] approval steps in order, then the payment step {label, userIds}
    (the last entry, type PAY; Form Fund only). Damaged Equipment: approval steps only, none = any monitoring account
    accepts as before. Requests waiting keep their position (clamped to the new number of steps)."""
    kinds, page = FLOW_FORMS.get(form, ({}, ""))
    purpose, whole = None, kind
    kind, _, unit = kind.partition("@")  # "<kind>@<unit>": the flow of one unit (app/fund_units.py)
    if unit:
        from app import fund_units
        if form != "pengajuan" or fund_units.get(unit) is None:
            raise PengajuanError("Unit tidak dikenal")
    if form == "pengajuan" and kind.startswith("PERSONAL:"):  # the flow of one Keperluan Dana
        from app import fund_options
        purpose = fund_options.get(int(kind.split(":", 1)[1]) if kind.split(":", 1)[1].isdigit() else 0)
        if purpose is None or purpose["list"] != "PURPOSE":
            raise PengajuanError("Keperluan Dana tidak dikenal")
    elif kind not in kinds:
        raise PengajuanError("Jenis pengajuan tidak dikenal")
    if unit and not steps:  # back to the flow of every unit
        with db.transaction() as conn:
            conn.execute(f"DELETE FROM {P}.approval_step WHERE form = %s AND kind = %s", (form, whole))
        return []
    appr = [s for s in steps if s.get("type") != "PAY"]
    pay = [s for s in steps if s.get("type") == "PAY"]
    if form == "pengajuan" and (not appr or len(pay) != 1):
        raise PengajuanError("Minimal satu tahap approval dan tepat satu tahap pembayaran")
    if form != "pengajuan" and pay:
        raise PengajuanError("Maintenance Request tidak memiliki tahap pembayaran")
    if len(appr) > MAX_STEPS:
        raise PengajuanError(f"Maksimal {MAX_STEPS} tahap approval")
    ids = {str(i) for s in steps for i in (s.get("userIds") or [])}
    if ids:
        known = {r["id"] for r in db.fetch(f"SELECT id::text FROM {P}.user_account WHERE id::text = ANY(%s) AND is_active", (list(ids),))}
        if ids - known:
            raise PengajuanError("Ada user approver yang tidak aktif atau tidak ditemukan")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {P}.approval_step WHERE form = %s AND kind = %s", (form, whole))
        for i, s in enumerate([*appr, *pay], start=1):
            label = " ".join(str(s.get("label") or "").split())[:60] or ("Pembayaran" if s.get("type") == "PAY" else f"Approval {i}")
            sid = conn.execute(f"""INSERT INTO {P}.approval_step (form, kind, position, label, step_type) VALUES (%s, %s, %s, %s, %s)
                                   RETURNING id""", (form, whole, i, label, "PAY" if s.get("type") == "PAY" else "APPROVE")).fetchone()["id"]
            for uid in dict.fromkeys(str(u) for u in (s.get("userIds") or [])):
                conn.execute(f"INSERT INTO {P}.approval_step_user (step_id, user_id) VALUES (%s, %s)", (sid, uid))
        # approvers open the monitoring (portal): an exception where their role does not give it
        for uid in ids:
            conn.execute(f"""INSERT INTO {P}.user_page (user_id, page, allowed) VALUES (%s, %s, true)
                             ON CONFLICT (user_id, page) DO UPDATE SET allowed = true""", (uid, page))
        if unit:
            conn.execute(f"""UPDATE {P}.pengajuan SET approval_step = LEAST(approval_step, %s) WHERE status = 'SUBMITTED' AND unit = %s
                             AND kind = %s AND (%s::int IS NULL OR purpose_id = %s)""",
                         (len(appr), unit, "PERSONAL" if purpose else kind, purpose["id"] if purpose else None, purpose["id"] if purpose else None))
        elif form == "pengajuan":
            if purpose:
                conn.execute(f"""UPDATE {P}.pengajuan SET approval_step = LEAST(approval_step, %s)
                                 WHERE kind = 'PERSONAL' AND purpose_id = %s AND status = 'SUBMITTED'""", (len(appr), purpose["id"]))
            else:
                conn.execute(f"""UPDATE {P}.pengajuan SET approval_step = LEAST(approval_step, %s) WHERE kind = %s AND status = 'SUBMITTED'""",
                             (len(appr), kind))
        else:
            conn.execute(f"""UPDATE {P}.de_request SET approval_step = GREATEST(LEAST(approval_step, %s), 1)
                             WHERE kind = %s AND status = 'REVIEW'""", (len(appr), kind))
    return flow(whole, form)


def approver_options() -> list[dict]:
    """Active personal accounts that can approve (not the shared outlet / hub accounts)."""
    rows = db.fetch(f"""SELECT u.id::text, COALESCE(NULLIF(u.full_name, ''), u.username) AS name, u.username, u.role, u.job_title
                        FROM {P}.user_account u WHERE u.is_active AND u.role <> 'outlet' ORDER BY 2""")
    return [{"id": r["id"], "name": r["name"], "username": r["username"], "role": r["role"], "jobTitle": r["job_title"]} for r in rows]


def outlet_options() -> list[dict]:
    """Every outlet that fills Form Fund (Kopi Calf and Wasgee Tea branches of ESB), for the plafon per outlet."""
    rows = db.fetch("""SELECT DISTINCT ON (branch_code) branch_code AS code, branch_name AS name FROM integration_esb.master_branches
                       WHERE COALESCE(branch_code, '') <> '' AND COALESCE(is_deleted, false) = false AND COALESCE(is_active, true)
                         AND (branch_name ILIKE 'Kopi Calf%%' OR branch_name ILIKE 'Wasgee%%') ORDER BY branch_code, branch_name""")
    return sorted(({"code": r["code"], "name": r["name"]} for r in rows), key=lambda r: r["name"])


def plafon_of(branch_code: str) -> Optional[Decimal]:
    r = db.fetchrow(f"""SELECT amount FROM {P}.pengajuan_plafon WHERE branch_code IN (%s, '*')
                        ORDER BY (branch_code = '*') LIMIT 1""", (branch_code,))
    return r["amount"] if r else None


def plafons() -> dict:
    rows = db.fetch(f"SELECT branch_code, amount, updated_at, updated_by FROM {P}.pengajuan_plafon ORDER BY branch_code")
    return {"default": next((_num(r["amount"]) for r in rows if r["branch_code"] == "*"), None),
            "branches": [{"branchCode": r["branch_code"], "amount": _num(r["amount"]), "updatedAt": _iso(r["updated_at"]),
                          "updatedBy": r["updated_by"]} for r in rows if r["branch_code"] != "*"]}


def set_plafon(user: dict, branch_code: str, amount: Any) -> dict:
    """amount None: the outlet follows the plafon of every outlet again (or '*': no plafon)."""
    branch_code = (branch_code or "").strip()[:40]
    if not branch_code:
        raise PengajuanError("Pilih outlet")
    value = _amount(amount, "Plafon")
    if value is None:
        db.fetchrow(f"DELETE FROM {P}.pengajuan_plafon WHERE branch_code = %s RETURNING branch_code", (branch_code,))
    else:
        db.fetchrow(f"""INSERT INTO {P}.pengajuan_plafon (branch_code, amount, updated_by) VALUES (%s, %s, %s)
                        ON CONFLICT (branch_code) DO UPDATE SET amount = EXCLUDED.amount, updated_at = now(), updated_by = EXCLUDED.updated_by
                        RETURNING branch_code""", (branch_code, value, _actor(user)))
    return plafons()


def requested(kind: str, opening: Any, income: Any, out: Any, plafon: Optional[Decimal]) -> Decimal:
    """What the outlet receives: petty cash with a plafon → the top-up back to it (never below 0); otherwise the money out."""
    out = Decimal(str(out or 0))
    if kind == "PETTY_CASH" and plafon is not None and opening is not None:
        return max(plafon - (Decimal(str(opening)) + Decimal(str(income or 0)) - out), Decimal(0))
    return out


def purchase_reference(branch_code: str, until: Optional[date] = None) -> Optional[dict]:
    """What the outlet bought itself in ESB (Simple Purchase, the purchases of the outlet, not the head office) over
    the 13 weeks before `until`: the reference of the weekly petty cash (MoM 2026-10-27)."""
    end = until or _now().date()
    b = db.fetchrow("""SELECT branch_id::text, branch_name FROM integration_esb.master_branches WHERE branch_code = %s
                       AND COALESCE(is_deleted, false) = false LIMIT 1""", (branch_code,))
    if not b:
        return None
    r = db.fetchrow("""SELECT COALESCE(sum(total), 0) AS total, count(*)::int AS docs,
                              COALESCE(sum(total) FILTER (WHERE doc_date > %(end)s - 7), 0) AS last_week
                       FROM integration_esb.erp_documents
                       WHERE module = 'simple_purchase' AND deleted_at IS NULL AND branch_id::text = %(id)s AND branch_name = %(name)s
                         AND doc_date > %(end)s - 91 AND doc_date <= %(end)s""", {"id": b["branch_id"], "name": b["branch_name"], "end": end})
    return {"weeks": 13, "until": end.isoformat(), "total": _num(r["total"]), "avgWeek": round(float(r["total"]) / 13),
            "lastWeek": _num(r["last_week"]), "docs": r["docs"]}


SP_WEEKS = 13
ABOVE_REF = 2  # a request above this many times the outlet's weekly average is flagged for the approvers


def _esb_branches(codes: list[str]) -> dict[str, dict]:
    """ESB branch id and name of the branch codes (Simple Purchase documents carry them)."""
    rows = db.fetch("""SELECT DISTINCT ON (branch_code) branch_code, branch_id::text, branch_name FROM integration_esb.master_branches
                       WHERE branch_code = ANY(%s) AND COALESCE(is_deleted, false) = false ORDER BY branch_code""", (codes,))
    return {r["branch_code"]: r for r in rows}


def weekly_averages(codes: list[str], until: Optional[date] = None) -> dict[str, dict]:
    """Per branch: the average a week of its Simple Purchase over the SP_WEEKS weeks before `until`, and its last 7 days."""
    if not codes:
        return {}
    end = until or _now().date()
    br = _esb_branches(codes)
    if not br:
        return {}
    keys = [f"{b['branch_id']}|{b['branch_name']}" for b in br.values()]
    rows = db.fetch("""SELECT branch_id::text || '|' || branch_name AS k, sum(total) AS total, count(*)::int AS docs,
                              COALESCE(sum(total) FILTER (WHERE doc_date > %(end)s - 7), 0) AS last_week
                       FROM integration_esb.erp_documents
                       WHERE module = 'simple_purchase' AND deleted_at IS NULL AND doc_date > %(end)s - %(days)s AND doc_date <= %(end)s
                         AND branch_id::text || '|' || branch_name = ANY(%(keys)s)
                       GROUP BY 1""", {"end": end, "days": SP_WEEKS * 7, "keys": keys})
    by_key = {r["k"]: r for r in rows}
    out = {}
    for code, b in br.items():
        r = by_key.get(f"{b['branch_id']}|{b['branch_name']}")
        total = float(r["total"]) if r else 0.0
        out[code] = {"weeks": SP_WEEKS, "until": end.isoformat(), "total": total, "avgWeek": round(total / SP_WEEKS),
                     "lastWeek": _num(r["last_week"]) if r else 0.0, "docs": r["docs"] if r else 0}
    return out


def purchase_docs(branch_code: str, date_from: Optional[date], date_to: Optional[date]) -> Optional[dict]:
    """The outlet's Simple Purchase documents of a period, for the reconciliation with its petty cash."""
    if not date_from or not date_to:
        return None
    b = _esb_branches([branch_code]).get(branch_code)
    if not b:
        return None
    rows = db.fetch("""SELECT doc_num, doc_date, counterparty_name, total, status_name FROM integration_esb.erp_documents
                       WHERE module = 'simple_purchase' AND deleted_at IS NULL AND branch_id::text = %s AND branch_name = %s
                         AND doc_date BETWEEN %s AND %s ORDER BY doc_date, doc_num LIMIT 300""",
                    (b["branch_id"], b["branch_name"], date_from, date_to))
    return {"from": date_from.isoformat(), "to": date_to.isoformat(), "total": sum(float(r["total"] or 0) for r in rows),
            "docs": [{"number": r["doc_num"], "date": _iso(r["doc_date"]), "supplier": r["counterparty_name"],
                      "total": _num(r["total"]), "status": r["status_name"]} for r in rows]}


def plafon_suggestions(factor: float = 1.2, round_to: int = 50_000) -> list[dict]:
    """A plafon for every outlet from its own purchases: the weekly average × factor, rounded up (the outlets that
    bought nothing in ESB get none)."""
    outlets = outlet_options()
    avgs = weekly_averages([o["code"] for o in outlets])
    have = {b["branchCode"]: b["amount"] for b in plafons()["branches"]}
    out = []
    for o in outlets:
        a = avgs.get(o["code"], {}).get("avgWeek", 0)
        sug = int(-(-a * factor // round_to) * round_to) if a else None
        out.append({"code": o["code"], "name": o["name"], "avgWeek": a, "suggested": sug, "current": have.get(o["code"])})
    return out


def apply_suggestions(user: dict, codes: list[str], factor: float, round_to: int) -> dict:
    """Sets the suggested plafon on the outlets given (Finance adjusts them afterwards one by one)."""
    sug = {s["code"]: s["suggested"] for s in plafon_suggestions(factor, round_to)}
    n = 0
    for code in codes:
        if sug.get(code):
            set_plafon(user, code, sug[code])
            n += 1
    return {"applied": n, **plafons()}


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
    from app import fund_units
    branches = [{"code": b["code"], "name": b["name"]} for b in branch_options(user)]
    mine_units = fund_units.requests_for(user, branches)
    rows = db.fetch(f"""SELECT {COLS} FROM {P}.pengajuan p WHERE p.user_id::text = %s
                          AND NOT (p.kind = 'VENDOR' AND p.status = 'DRAFT')  -- the vendor drafts have their own list (fund_vendor)
                        ORDER BY (p.status IN ('DRAFT', 'REVISION')) DESC, COALESCE(p.submitted_at, p.updated_at) DESC LIMIT 60""",
                    (str(user["id"]),))
    return {"user": {"id": str(user["id"]), "username": user["username"], "name": user.get("full_name") or user["username"]},
            "units": [u for u in fund_units.units() if u["code"] in set(mine_units)],
            "branches": [{**b, "unit": fund_units.unit_of_branch(b["code"])} for b in branches], "requests": [view(r) for r in rows], "categories": CATEGORIES, "categoriesOf": CATEGORIES_OF,
            "kinds": KINDS, "neededOn": NEEDED_ON, "receiptRequired": list(RECEIPT_REQUIRED), "docTypes": DOC_TYPES,
            "statusLabel": STATUS_LABEL, "payers": _payers(), "baristas": {b["code"]: barista_options(b["code"]) for b in branches},
            "kindLabels": kind_labels(), **personal_state(user)}


def previous_closing(branch_code: str, exclude: Optional[str] = None) -> Optional[dict]:
    """The branch's latest sent petty cash (not rejected): its closing balance is the next opening balance, the next
    period starts the day after its end."""
    return db.fetchrow(f"""SELECT closing_balance, period_to FROM {P}.pengajuan WHERE branch_code = %s AND kind = 'PETTY_CASH'
                             AND status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED') AND (%s::text IS NULL OR id::text <> %s) AND closing_balance IS NOT NULL
                           ORDER BY period_to DESC NULLS LAST, first_submitted_at DESC LIMIT 1""", (branch_code, exclude, exclude))


def create_draft(user: dict, branch_code: Optional[str], kind: str = "PETTY_CASH", unit: Optional[str] = None) -> dict:
    if kind not in KINDS or kind in DE_KINDS:  # Maintenance & Procurement funds come from their request (de_funds)
        raise PengajuanError("Jenis pengajuan tidak dikenal")
    if kind in ONE_AMOUNT:
        return create_personal(user, branch_code, kind, unit)
    branch = next((b for b in branch_options(user) if b["code"] == branch_code), None)
    if branch is None:
        raise PengajuanError("Cabang tidak tersedia untuk akun Anda")
    row = db.fetchrow(f"""SELECT id::text FROM {P}.pengajuan WHERE user_id::text = %s AND branch_code = %s AND kind = %s AND status = 'DRAFT'
                          ORDER BY updated_at DESC LIMIT 1""", (str(user["id"]), branch_code, kind))
    if row is None:
        # the requester and the bank account of the branch's last request are suggested
        last = db.fetchrow(f"""SELECT requester_name, requester_phone, payee_bank, payee_account, payee_name FROM {P}.pengajuan
                               WHERE branch_code = %s AND status <> 'DRAFT' ORDER BY first_submitted_at DESC LIMIT 1""", (branch_code,)) or {}
        shared = user["username"].split(".")[0] in ("outlet", "wasgee")
        me = {"name": user.get("full_name") or user["username"]}
        opening = period_from = period_to = None
        if kind == "PETTY_CASH":
            prev = previous_closing(branch_code)
            opening = prev["closing_balance"] if prev else None
            today = _now().date()
            period_to = today
            period_from = today - timedelta(days=today.weekday())  # Monday of this week
            if prev and prev["period_to"] and prev["period_to"] < today and (today - prev["period_to"]).days <= 14:
                period_from = prev["period_to"] + timedelta(days=1)
        row = db.fetchrow(f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, branch_code, branch_name, baristas, kind,
                                  opening_balance, opening_expected, closing_balance, period_from, period_to, requester_name,
                                  requester_phone, payee_bank, payee_account, payee_name)
                              VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                          (user["id"], user["username"], user.get("full_name"), branch["code"], branch["name"],
                           json.dumps([] if shared or kind != "PETTY_CASH" else [me]), kind, opening, opening, opening,
                           period_from, period_to, last.get("requester_name") or (None if shared else me["name"]),
                           last.get("requester_phone"), last.get("payee_bank"), last.get("payee_account"), last.get("payee_name")))
    from app import fund_units
    with db.transaction() as conn:
        fund_units.stamp(conn, row["id"], fund_units.unit_of_branch(branch_code))
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
    row = own(user, pengajuan_id, editable=True)
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
    for key, col in (("periodFrom", "period_from"), ("periodTo", "period_to"), ("neededOn", "needed_on")):
        if key in body:
            sets[col] = _date(body[key])
    if "openingBalance" in body:
        sets["opening_balance"] = _amount(body["openingBalance"], "Saldo awal", allow_negative=True)
    if "incomeAmount" in body:
        sets["income_amount"] = _amount(body["incomeAmount"], "Dana masuk") or Decimal(0)
    if row["kind"] in ONE_AMOUNT:
        sets.update(personal_sets(user, row, body))
    for key, col in (("openingNote", "opening_note"), ("incomeNote", "income_note"), ("note", "note"), ("purpose", "purpose")):
        if key in body:
            sets[col] = (str(body[key] or "")).strip()[:1000] or None
    if "paymentTerm" in body:
        if body["paymentTerm"] not in (None, "", *TERMS):
            raise PengajuanError("Termin pembayaran tidak dikenal")
        sets["payment_term"] = body["paymentTerm"] or None
    if "invoiceDate" in body:
        sets["invoice_date"] = _date(body["invoiceDate"])
    if "outstandingAmount" in body:
        sets["outstanding_amount"] = _amount(body["outstandingAmount"], "Total outstanding invoice")
    if "payeeType" in body:
        if body["payeeType"] not in ("BANK", "VA"):
            raise PengajuanError("Jenis rekening tujuan tidak dikenal")
        sets["payee_type"] = body["payeeType"]
    for key, col, size in (("requesterName", "requester_name", 80), ("requesterPhone", "requester_phone", 30), ("payeeBank", "payee_bank", 60),
                           ("payeeAccount", "payee_account", 40), ("payeeName", "payee_name", 80), ("vaProvider", "va_provider", 60),
                           ("invoiceNo", "invoice_no", 80), ("vendorName", "vendor_name", 120), ("payeePt", "payee_pt", 80)):
        if key in body:
            sets[col] = " ".join(str(body[key] or "").split())[:size] or None
    lines = body.get("lines")
    if lines is not None and len(lines) > MAX_LINES:
        raise PengajuanError(f"Maksimal {MAX_LINES} pengeluaran per pengajuan")
    clean = []
    for i, ln in enumerate(lines or [], start=1):
        cat = ln.get("category") or "OTHER"
        if cat not in CATEGORIES_OF[row["kind"]]:
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
    kind = row.get("kind") or "PETTY_CASH"
    if kind == "PERSONAL":
        return missing_personal(row, files)
    if kind == "VENDOR":
        return missing_vendor(row, files)
    if kind == "INTERCOMPANY":
        return missing_intercompany(row, files, _payers())
    petty = kind == "PETTY_CASH"
    out = []
    if not (row.get("requester_name") or "").strip():
        out.append("Isi nama pengaju (barista / PIC)")
    phone = "".join(ch for ch in (row.get("requester_phone") or "") if ch.isdigit())
    if len(phone) < 9:
        out.append("Isi no. handphone pribadi pengaju")
    if not (row.get("payee_bank") or "").strip() or not (row.get("payee_account") or "").strip() or not (row.get("payee_name") or "").strip():
        out.append("Isi rekening tujuan: bank, no. rekening dan nama pemilik")
    if petty:
        if not row["baristas"]:
            out.append("Isi minimal satu nama barista")
    elif not (row.get("purpose") or "").strip():
        out.append("Tulis keperluan pengajuan")
    if kind in NEEDED_ON and not row.get("needed_on"):
        out.append(f"Isi {NEEDED_ON[kind].lower()}")
    pf, pt = row["period_from"], row["period_to"]
    if petty:
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
        out.append("Tambahkan minimal satu pengeluaran" if petty or kind == "EMERGENCY" else "Tambahkan minimal satu rincian biaya")
    with_file = {f["lineId"] for f in files if f["kind"] == "LINE"}
    for i, ln in enumerate(lines, start=1):
        problems = []
        if kind in LINE_DATE_REQUIRED and not ln["spentOn"]:
            problems.append("tanggal")
        elif petty and pf and pt and ln["spentOn"] and not (pf.isoformat() <= ln["spentOn"] <= pt.isoformat()):
            problems.append("tanggal di luar periode")
        if not ln["description"]:
            problems.append("keterangan")
        if not ln["amount"] or ln["amount"] <= 0:
            problems.append("nominal")
        if kind in RECEIPT_REQUIRED and ln["id"] not in with_file:
            problems.append("nota / kwitansi / invoice")
        if problems:
            out.append(f"{'Pengeluaran' if petty or kind == 'EMERGENCY' else 'Rincian'} {i}: {', '.join(problems)}")
    if petty and row["closing_balance"] is not None and row["closing_balance"] < 0 and not (row["note"] or "").strip():
        out.append("Saldo akhir minus: tulis penjelasan di catatan")
    return out


def overlapping(row: dict) -> Optional[str]:
    """Number of another petty cash of the branch whose period overlaps this one (one per branch per period)."""
    if row.get("kind") != "PETTY_CASH" or not row["period_from"] or not row["period_to"]:
        return None
    r = db.fetchrow(f"""SELECT number FROM {P}.pengajuan WHERE branch_code = %s AND kind = 'PETTY_CASH' AND id::text <> %s
                          AND status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED') AND period_from <= %s AND period_to >= %s LIMIT 1""",
                    (row["branch_code"], row["id"], row["period_to"], row["period_from"]))
    return r["number"] if r else None


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
    from app import fund_lifecycle
    todo = missing(row, lines, files) + fund_lifecycle.check_send(row, files, today_wib())
    if todo:
        raise PengajuanError("Belum bisa dikirim: " + "; ".join(todo))
    other = overlapping(row)
    if other:
        raise PengajuanError(f"Periode ini sudah diajukan di {other}: petty cash satu kali per periode, pilih periode berikutnya")
    from app import fund_units
    unit = (row.get("unit") or fund_units.MAIN) if row["kind"] in ONE_AMOUNT else fund_units.unit_of_branch(row["branch_code"])
    if row["kind"] in ONE_AMOUNT and row["status"] == "DRAFT" and unit not in fund_units.requests_for(user, branch_options(user)):
        raise PengajuanError("Pilih unit yang mengajukan (Roastery, Warehouse, Central Kitchen …)")
    if row["kind"] == "INTERCOMPANY":
        twice = same_period(row)
        if twice:
            raise PengajuanError(f"Periode ini sudah diajukan di {', '.join(twice)} (unit, lokasi dan PT tujuan sama)")
    if row["kind"] == "VENDOR":
        twice = same_invoice(row)
        if twice:
            raise PengajuanError(f"Invoice ini sudah diajukan di {', '.join(twice)} (rekening, nomor invoice dan nominal sama)")
    plafon = plafon_of(row["branch_code"]) if row["kind"] == "PETTY_CASH" else None
    ask = requested(row["kind"], row["opening_balance"], row["income_amount"], row["total_out"], plafon)
    if row["kind"] == "PERSONAL":
        plafon, ask = check_personal(row), row["requested_amount"]
    if row["kind"] in ("VENDOR", "INTERCOMPANY"):
        plafon, ask = None, row["requested_amount"]
    with db.transaction() as conn:
        number = row["number"] or _next_number(conn)
        # a revision is checked again from the first approval step
        conn.execute(f"UPDATE {P}.pengajuan_line SET review = 'OK', review_note = NULL, reviewed_by = NULL WHERE pengajuan_id::text = %s",
                     (pengajuan_id,))
        conn.execute(f"""UPDATE {P}.pengajuan SET number = %s, status = 'SUBMITTED', approval_step = 1, submitted_at = now(),
                             first_submitted_at = COALESCE(first_submitted_at, now()), approved_at = NULL, approved_by = NULL,
                             approved_amount = NULL, plafon = %s, requested_amount = %s, updated_at = now() WHERE id::text = %s""",
                     (number, plafon, ask, pengajuan_id))
        fund_units.stamp(conn, pengajuan_id, unit)
        _event(conn, pengajuan_id, user, "SUBMIT", row["status"], "SUBMITTED", None,
               {"totalOut": row["total_out"], "requested": ask, "lines": len(lines), "files": len(files),
                "resubmit": row["status"] == "REVISION"})
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

def allowed_next(user: dict, row: dict, steps: Optional[list[dict]] = None) -> list[str]:
    if row["status"] not in ("SUBMITTED", "APPROVED") or not takes(user, current_step(row, steps)):
        return []
    return ["APPROVE", "REVISION", "REJECTED"] if row["status"] == "SUBMITTED" else ["PAID", "REVISION", "CANCELLED"]


def can(user: dict, row: dict) -> dict:
    """What the signed-in user may do on it now, and who takes the step waiting."""
    steps = steps_of(row)
    step = current_step(row, steps)
    mine = takes(user, step)
    refund = row["status"] in ("PAID", "VERIFYING") and row.get("refund_amount") is not None and not row.get("refunded_at")
    return {"receive": can_receive(user, row), "next": allowed_next(user, row, steps), "reviewLines": mine and row["status"] == "SUBMITTED",
            "verify": row["status"] == "VERIFYING" and takes(user, _pay(steps)),
            "payment": mine and row["status"] == "APPROVED", "step": step["label"] if step else None,
            "refund": refund and takes(user, _pay(steps)),
            "stepUsers": [u["name"] for u in step["users"]] if step else []}


def confirm_refund(user: dict, pengajuan_id: str, note: Optional[str]) -> dict:
    """Finance (the payment step) confirms the money a Maintenance & Procurement fund gave back (app/de_funds.py)."""
    row = get_row(pengajuan_id)
    if row is None or row["status"] not in ("PAID", "VERIFYING") or row.get("refund_amount") is None:
        raise LookupError("Pengajuan tidak ditemukan")
    if row.get("refunded_at"):
        raise PengajuanError("Pengembalian dana sudah dikonfirmasi")
    if not takes(user, _pay(steps_of(row))):
        raise PermissionError("Konfirmasi pengembalian oleh tahap pembayaran Finance")
    if not any(f["kind"] == "REFUND" for f in files_of(pengajuan_id)):
        raise PengajuanError("Lampirkan bukti dana kembali dulu")
    note = (note or "").strip()[:2000] or None
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.pengajuan SET refunded_at = now(), refunded_by = %s, refunded_note = %s, updated_at = now()
                         WHERE id::text = %s""", (_actor(user), note, pengajuan_id))
        _event(conn, pengajuan_id, user, "REFUND", row["status"], row["status"], note, {"refund": row["refund_amount"]})
        from app import de_funds
        de_funds.on_fund_status(conn, row, user, "REFUNDED", note, {"amount": row["refund_amount"]})
    return full(pengajuan_id)


def change_status(user: dict, pengajuan_id: str, to: str, note: Optional[str], paid: Optional[dict] = None) -> dict:
    """to: APPROVE (the approval step waiting; the last one makes it APPROVED), REVISION, REJECTED, PAID (payment step)."""
    row = get_row(pengajuan_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if to not in allowed_actions(row["status"]):
        raise PengajuanError(f"Tidak dapat memproses pengajuan {STATUS_LABEL[row['status']].lower()} dengan langkah ini")
    steps = steps_of(row)
    step = current_step(row, steps)
    if not takes(user, step):
        who = ", ".join(u["name"] for u in step["users"]) if step and step["users"] else "superadmin"
        raise PermissionError(f"Tahap {step['label'] if step else '—'} bukan bagian Anda (approver: {who})")
    note = (note or "").strip()[:2000] or None
    if to in NOTE_REQUIRED and not note:
        raise PengajuanError("Tulis alasannya agar outlet tahu apa yang harus diperbaiki" if to == "REVISION" else "Tulis alasan pembatalan" if to == "CANCELLED" else "Tulis alasan penolakan")
    details: dict[str, Any] = {"step": step["label"], "action": to}
    sets: list[str] = []
    status, position = to, row["approval_step"]
    if to == "APPROVE":
        if row["approval_step"] >= len(_approvals(steps)):
            if row["kind"] in ONE_AMOUNT:  # one amount, no expense lines
                details["approvedAmount"] = row["requested_amount"]
            else:
                ok = sum((Decimal(str(ln["amount"] or 0)) for ln in lines_of(pengajuan_id) if ln["review"] == "OK"), Decimal(0))
                details["approvedAmount"] = requested(row["kind"], row["opening_balance"], row["income_amount"], ok, row["plafon"])
            sets += ["approved_at = now()", "approved_by = %(actor)s", "approved_amount = %(approved)s"]
            status = "APPROVED"
        else:
            status, position = "SUBMITTED", row["approval_step"] + 1
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
        from app import fund_lifecycle
        status, due = ("PAID", None) if row["kind"] == "INTERCOMPANY" else fund_lifecycle.after_paid(fund_lifecycle.rule_for(row), on)
        details["proofDueOn"] = due
        sets += ["paid_at = now()", "paid_by = %(actor)s", "paid_amount = %(paid)s", "paid_on = %(paid_on)s", "paid_note = %(note)s",
                 "proof_due_on = %(due)s"] + (["closed_at = now()", "done_at = now()"] if status == "DONE" else [])
    if to in ("REJECTED", "CANCELLED"):
        sets.append("closed_at = now()")
    params = {"id": pengajuan_id, "to": status, "pos": position, "actor": _actor(user), "approved": details.get("approvedAmount"),
              "paid": details.get("amount"), "paid_on": details.get("on"), "note": note, "due": details.get("proofDueOn")}
    with db.transaction() as conn:
        # two people at once: only the first takes the step (a payment is never recorded twice)
        hit = conn.execute(f"""UPDATE {P}.pengajuan SET status = %(to)s, approval_step = %(pos)s, updated_at = now(){''.join(', ' + s for s in sets)}
                               WHERE id::text = %(id)s AND status = %(was)s AND approval_step = %(waspos)s RETURNING id""",
                           {**params, "was": row["status"], "waspos": row["approval_step"]}).fetchone()
        if hit is None:
            raise PengajuanError("Pengajuan baru saja diproses orang lain; muat ulang")
        _event(conn, pengajuan_id, user, "STATUS", row["status"], status, note, details)
        from app import de_funds
        de_funds.on_fund_status(conn, row, user, status, note, details)
    return full(pengajuan_id)


def cancel(user: dict, pengajuan_id: str, note: Optional[str]) -> dict:
    """The requester withdraws the request before the money goes out (2026-11-24); a reason is required."""
    row = own(user, pengajuan_id)
    if row["status"] not in CANCELLABLE:
        raise PengajuanError("Pengajuan dibatalkan sebelum dana dicairkan")
    note = (note or "").strip()[:2000] or None
    if not note:
        raise PengajuanError("Tulis alasan pembatalan")
    from app import de_funds
    with db.transaction() as conn:
        hit = conn.execute(f"""UPDATE {P}.pengajuan SET status = 'CANCELLED', closed_at = now(), updated_at = now()
                               WHERE id::text = %s AND status = ANY(%s) RETURNING id""", (pengajuan_id, list(CANCELLABLE))).fetchone()
        if hit is None:
            raise PengajuanError("Pengajuan baru saja diproses; muat ulang")
        _event(conn, pengajuan_id, user, "STATUS", row["status"], "CANCELLED", note, {"action": "CANCELLED", "byRequester": True})
        de_funds.on_fund_status(conn, row, user, "CANCELLED", note, {})
    return full(pengajuan_id)


def allowed_actions(status: str) -> tuple[str, ...]:
    return {"SUBMITTED": ("APPROVE", "REVISION", "REJECTED"), "APPROVED": ("PAID", "REVISION", "CANCELLED")}.get(status, ())


def review_line(user: dict, pengajuan_id: str, line_id: int, review: str, note: Optional[str]) -> dict:
    row = get_row(pengajuan_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if row["status"] != "SUBMITTED":
        raise PengajuanError("Rincian diperiksa saat approval")
    if not takes(user, current_step(row)):
        raise PermissionError("Pemeriksaan di tahap ini bukan bagian Anda")
    if review not in ("OK", "REJECTED"):
        raise PengajuanError("Hasil pemeriksaan harus OK atau REJECTED")
    note = (note or "").strip()[:500] or None
    if review == "REJECTED" and not note:
        raise PengajuanError("Tulis alasan rincian ini ditolak")
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


def monitor(date_from: date, date_to: date, branches: Optional[list[str]], units: Optional[list[str]] = None, user: Optional[dict] = None) -> dict:
    """Sent pengajuan of the period (first sending, WIB days) and every one still open, with their expenses."""
    where = ["p.status <> 'DRAFT'",
             "((p.first_submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s OR p.status = ANY(%(open)s))"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "open": list(OPEN)}
    if branches is not None:
        where.append("(p.branch_code = ANY(%(br)s) OR p.branch_code IS NULL)")  # personal: no outlet
        params["br"] = branches
    if units is not None:
        where.append("coalesce(p.unit, 'CALF') = ANY(%(units)s)")
        params["units"] = units
    rows = db.fetch(f"SELECT {COLS} FROM {P}.pengajuan p WHERE {' AND '.join(where)} ORDER BY p.submitted_at DESC LIMIT 2000", params)
    ids = [r["id"] for r in rows]
    lines: dict[str, list] = {}
    trx = _option_names()
    stats: dict[str, dict] = {}
    if ids:
        for ln in db.fetch(f"""SELECT pengajuan_id::text AS pid, id, spent_on, category, description, amount, review, review_note
                               FROM {P}.pengajuan_line WHERE pengajuan_id::text = ANY(%s) ORDER BY sort, id""", (ids,)):
            lines.setdefault(ln["pid"], []).append({"id": ln["id"], "spentOn": _iso(ln["spent_on"]), "category": ln["category"],
                                                    "categoryLabel": category_label(ln["category"], trx),
                                                    "description": ln["description"], "amount": _num(ln["amount"]), "review": ln["review"],
                                                    "reviewNote": ln["review_note"]})
        for s in db.fetch(f"""SELECT f.pengajuan_id::text AS pid, count(*)::int AS files,
                                     count(*) FILTER (WHERE EXISTS (SELECT 1 FROM {P}.pengajuan_file o JOIN {P}.pengajuan q ON q.id = o.pengajuan_id
                                         WHERE o.sha256 = f.sha256 AND o.pengajuan_id <> f.pengajuan_id AND q.status <> 'DRAFT' AND f.kind <> 'PAYMENT'))::int AS reused
                              FROM {P}.pengajuan_file f WHERE f.pengajuan_id::text = ANY(%s) GROUP BY 1""", (ids,)):
            stats[s["pid"]] = s
    flows = {(flow_key(r), r["unit"]): steps_of(r) for r in {(flow_key(x), x["unit"]): x for x in rows}.values()}
    # in approval and not reached by this user: not listed, only counted (the cash Finance will be asked for)
    hidden = [r for r in rows if user is not None and not sees(user, r, flows[(flow_key(r), r["unit"])])]
    gone = {r["id"] for r in hidden}
    rows = [r for r in rows if r["id"] not in gone]
    pipeline = {"count": len(hidden), "amount": float(sum((r["requested_amount"] or 0) for r in hidden)),
                "byUnit": {u: sum(1 for r in hidden if (r["unit"] or "CALF") == u) for u in sorted({r["unit"] or "CALF" for r in hidden})}}
    twins = twin_ids()
    refs = weekly_averages(sorted({r["branch_code"] for r in rows if r["kind"] == "PETTY_CASH"}))
    data = []
    for r in rows:
        v = view(r)
        step = current_step(r, flows[(flow_key(r), r["unit"])])
        v["step"] = {"label": step["label"], "users": [u["name"] for u in step["users"]],
                     "userIds": [u["id"] for u in step["users"]]} if step else None
        ref = refs.get(r["branch_code"]) if r["kind"] == "PETTY_CASH" else None
        v["purchaseAvgWeek"] = ref["avgWeek"] if ref else None
        asked = r["approved_amount"] if r["approved_amount"] is not None else r["requested_amount"]
        v["aboveRef"] = bool(ref and ref["avgWeek"] and asked and float(asked) > ABOVE_REF * ref["avgWeek"])
        v["lines"] = lines.get(r["id"], [])
        v["fileCount"] = stats.get(r["id"], {}).get("files", 0)
        v["twin"] = r["id"] in twins
        v["reusedFiles"] = stats.get(r["id"], {}).get("reused", 0)
        data.append(v)
    from app import fund_options
    return {"data": data, "pipeline": pipeline, "categories": CATEGORIES, "kinds": KINDS, "docTypes": DOC_TYPES, "statusLabel": STATUS_LABEL,
            "options": fund_options.all_options(), "kindLabels": kind_labels()}


# ---------------------------------------------------------------- personal requests (2026-11-20, migration 124)

def _option_names() -> dict[int, str]:
    return {r["id"]: r["name"] for r in db.fetch(f"SELECT id, name FROM {P}.fund_option")}


def _profile(user: dict) -> dict:
    """Email, name, department and job title of the account (synced from HRMS): filled in and not typed again."""
    return {"email": user.get("email"), "name": user.get("full_name") or user["username"], "department": user.get("department"),
            "jobTitle": user.get("job_title")}


def open_advances(user_id: str, exclude: Optional[str] = None) -> list[dict]:
    """The Cash in Advance of a person paid and not settled yet (a settlement not rejected settles it)."""
    rows = db.fetch(f"""SELECT p.id::text, p.number, p.paid_amount, p.paid_on, p.purpose FROM {P}.pengajuan p
                         JOIN {P}.fund_option o ON o.id = p.purpose_id AND o.settings->>'type' = 'ADVANCE'
                        WHERE p.kind = 'PERSONAL' AND p.user_id::text = %s AND p.status = 'PAID'
                          AND NOT EXISTS (SELECT 1 FROM {P}.pengajuan s WHERE s.settles_id = p.id AND s.status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED')
                                            AND (%s::text IS NULL OR s.id::text <> %s))
                        ORDER BY p.paid_on DESC NULLS LAST""", (user_id, exclude, exclude))
    return [{"id": r["id"], "number": r["number"], "paidAmount": _num(r["paid_amount"]), "paidOn": _iso(r["paid_on"]), "purpose": r["purpose"]} for r in rows]


def settle_diff(paid: Any, spent: Any) -> dict:
    """A Laporan Penyelesaian: spent − paid > 0 the company still pays, < 0 the person gives back (pure)."""
    d = Decimal(str(spent or 0)) - Decimal(str(paid or 0))
    return {"diff": float(d), "outcome": "PAY" if d > 0 else "RETURN" if d < 0 else "EVEN"}


def settlement_of(row: dict) -> Optional[dict]:
    if not row.get("settles_id"):
        return None
    ca = db.fetchrow(f"SELECT id::text, number, paid_amount, paid_on FROM {P}.pengajuan WHERE id::text = %s", (row["settles_id"],))
    if ca is None:
        return None
    return {"id": ca["id"], "number": ca["number"], "paidAmount": _num(ca["paid_amount"]), "paidOn": _iso(ca["paid_on"]),
            **settle_diff(ca["paid_amount"], row["requested_amount"])}


def personal_state(user: dict) -> dict:
    from app import fund_options
    last = db.fetchrow(f"""SELECT payee_bank, payee_account, payee_name, account_type_id FROM {P}.pengajuan
                           WHERE user_id::text = %s AND kind = 'PERSONAL' AND status <> 'DRAFT' ORDER BY first_submitted_at DESC LIMIT 1""",
                       (str(user["id"]),)) or {}
    return {"profile": _profile(user), "options": fund_options.all_options(active_only=True), "outlets": outlet_options(), "listNames": list_names(),
            "advances": open_advances(str(user["id"])),
            "lastPayee": {"bank": last.get("payee_bank"), "account": last.get("payee_account"), "name": last.get("payee_name"),
                          "accountTypeId": last.get("account_type_id")}}


def create_personal(user: dict, branch_code: Optional[str], kind: str = "PERSONAL", unit: Optional[str] = None) -> dict:
    """A personal request or a vendor payment: both have one amount, the identity of the account and a unit."""
    from app import fund_units
    allowed = fund_units.requests_for(user, branch_options(user))
    if kind in ("VENDOR", "INTERCOMPANY"):
        allowed = [c for c in allowed if (fund_units.get(c) or {}).get("vendor")]
        if not allowed:
            raise PengajuanError("Pembayaran vendor dan Antar-PT untuk unit Roastery, Warehouse dan Central Kitchen")
    if unit and unit not in allowed:
        raise PengajuanError("Unit di luar akses Anda")
    unit = unit or (allowed[0] if len(allowed) == 1 else None)
    if unit is None:
        raise PengajuanError("Pilih unit yang mengajukan")
    row = db.fetchrow(f"""SELECT id::text FROM {P}.pengajuan WHERE user_id::text = %s AND kind = %s AND status = 'DRAFT'
                            AND coalesce(unit, 'CALF') = %s ORDER BY updated_at DESC LIMIT 1""", (str(user["id"]), kind, unit))
    if row is None:
        me, last = _profile(user), personal_state(user)["lastPayee"]
        mine = [b for b in branch_options(user) if fund_units.unit_of_branch(b["code"]) == unit]
        if kind != "PERSONAL":  # another vendor every time: no account is suggested
            last = {"bank": None, "account": None, "name": None, "accountTypeId": None}
        branch = next((b for b in mine if b["code"] == branch_code), None) or (mine[0] if len(mine) == 1 else None)
        row = db.fetchrow(f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, kind, baristas, email, department, job_title,
                                  requester_name, branch_code, branch_name, payee_bank, payee_account, payee_name, account_type_id)
                              VALUES (%s, %s, %s, %s, '[]'::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                          (user["id"], user["username"], user.get("full_name"), kind, me["email"], me["department"], me["jobTitle"], me["name"],
                           branch["code"] if branch else None, branch["name"] if branch else None,
                           last["bank"], last["account"], last["name"], last["accountTypeId"]))
        with db.transaction() as conn:
            fund_units.stamp(conn, row["id"], unit)
    return full(row["id"])


def missing_vendor(row: dict, files: list[dict]) -> list[str]:
    """What blocks a vendor payment. A due date already past is allowed (more than half of the Warehouse's are): flagged."""
    out = []
    for key, what in (("vendor_name", "Isi nama vendor"), ("payment_term", "Pilih termin (COD / TOP / CBD)"),
                      ("invoice_no", "Isi nomor invoice / nota (tulis - bila tidak ada)"), ("invoice_date", "Isi tanggal invoice"),
                      ("needed_on", "Isi tanggal jatuh tempo"), ("purpose", "Tulis deskripsi dan detail keperluan")):
        if not str(row.get(key) or "").strip():
            out.append(what)
    if row.get("invoice_date") and row.get("needed_on") and row["needed_on"] < row["invoice_date"]:
        out.append("Jatuh tempo sebelum tanggal invoice")
    if row.get("requested_amount") is None or row["requested_amount"] <= 0:
        out.append("Isi total dana diajukan")
    elif row.get("outstanding_amount") is not None and row["requested_amount"] > row["outstanding_amount"]:
        out.append("Dana diajukan melebihi total outstanding invoice")
    if row.get("payee_type") == "VA":
        if not (row.get("payee_account") or "").strip() or not ((row.get("va_provider") or row.get("payee_bank") or "").strip()):
            out.append("Isi Virtual Account tujuan: penyedia dan nomor VA")
    elif not all((row.get(k) or "").strip() for k in ("payee_bank", "payee_account", "payee_name")):
        out.append("Isi rekening tujuan: bank, nomor rekening dan nama pemilik")
    if not any(f["kind"] == "OTHER" for f in files):
        out.append("Lampirkan invoice / nota / dokumen pendukung")
    return out


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isalnum()).upper()


def same_invoice(row: dict) -> list[str]:
    """Other live requests with the same account, invoice number and amount: the same invoice paid twice."""
    if not _digits(row.get("invoice_no")) or row.get("requested_amount") is None:
        return []
    rows = db.fetch(f"""SELECT number, payee_account, invoice_no FROM {P}.pengajuan WHERE id::text <> %s AND requested_amount = %s
                          AND status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED')""", (row["id"], row["requested_amount"]))
    outside = db.fetch(f"SELECT label FROM {P}.fund_external WHERE account = %s AND invoice = %s AND amount = %s",
                       (_digits(row.get("payee_account")), _digits(row["invoice_no"]), row["requested_amount"]))
    return [r["number"] for r in rows if _digits(r["payee_account"]) == _digits(row.get("payee_account"))
            and _digits(r["invoice_no"]) == _digits(row["invoice_no"])] + [r["label"] for r in outside]


# ---------------------------------------------------------------- Antar-PT (migration 150): money between the PTs, no cost

def _payers() -> list[str]:
    from app import fund_units
    return list(dict.fromkeys(u["payer"] for u in fund_units.units()))


def missing_intercompany(row: dict, files: list[dict], payers: list[str]) -> list[str]:
    """What blocks an Antar-PT request: the receiving PT (another one than the payer), its period, the report and the account."""
    out = []
    if row.get("payee_pt") not in payers:
        out.append("Pilih PT penerima")
    elif row["payee_pt"] == row.get("payer"):
        out.append("PT penerima harus berbeda dari PT pembayar")
    a, z = row.get("period_from"), row.get("period_to")
    if not a or not z:
        out.append("Isi periode laporan (dari – sampai)")
    elif a > z:
        out.append("Tanggal mulai periode harus sebelum tanggal akhir")
    elif z > _now().date():
        out.append("Periode tidak boleh melewati hari ini")
    if not (row.get("purpose") or "").strip():
        out.append("Tulis deskripsi (mis. Purchase Return periode …)")
    if row.get("requested_amount") is None or row["requested_amount"] <= 0:
        out.append("Isi total dana diajukan")
    if not all((row.get(k) or "").strip() for k in ("payee_bank", "payee_account", "payee_name")):
        out.append("Isi rekening tujuan: bank, nomor rekening dan nama pemilik")
    if not any(f["kind"] == "OTHER" for f in files):
        out.append("Lampirkan laporan retur ESB")
    return out


def same_period(row: dict) -> list[str]:
    """Other live Antar-PT requests of the unit to the same PT whose period overlaps, at the same place (none = the whole unit)."""
    return [r["number"] for r in db.fetch(
        f"""SELECT number FROM {P}.pengajuan WHERE kind = 'INTERCOMPANY' AND id::text <> %s AND status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED')
              AND unit = %s AND payee_pt = %s AND period_from <= %s AND period_to >= %s
              AND (branch_code IS NULL OR %s::text IS NULL OR branch_code = %s)""",
        (row["id"], row.get("unit"), row.get("payee_pt"), row["period_to"], row["period_from"], row.get("branch_code"), row.get("branch_code")))]


def receivers(payer: Optional[str]) -> list[str]:
    return [str(r["user_id"]) for r in db.fetch(f"SELECT user_id FROM {P}.fund_receiver WHERE payer = %s", (payer,))]


def can_receive(user: dict, row: dict) -> bool:
    """The receiving PT confirms the money: its accounts (none set: superadmins), never who recorded the payment."""
    if row["kind"] != "INTERCOMPANY" or row["status"] != "PAID" or row.get("paid_by") == _actor(user):
        return False
    return user.get("role") == "superadmin" or str(user["id"]) in receivers(row.get("payee_pt"))


def confirm_received(user: dict, pengajuan_id: str, note: Optional[str]) -> dict:
    row = get_row(pengajuan_id)
    if row is None or row["kind"] != "INTERCOMPANY" or row["status"] != "PAID":
        raise LookupError("Pengajuan tidak ditemukan")
    if not can_receive(user, row):
        raise PermissionError("Uang masuk dikonfirmasi Finance PT penerima, bukan yang mencatat pembayaran")
    note = (note or "").strip()[:2000] or None
    with db.transaction() as conn:
        hit = conn.execute(f"""UPDATE {P}.pengajuan SET status = 'DONE', done_at = now(), closed_at = now(), received_at = now(), received_by = %s,
                                   received_note = %s, updated_at = now() WHERE id::text = %s AND status = 'PAID' RETURNING id""",
                           (_actor(user), note, pengajuan_id)).fetchone()
        if hit is None:
            raise PengajuanError("Pengajuan baru saja diproses; muat ulang")
        _event(conn, pengajuan_id, user, "STATUS", "PAID", "DONE", note, {"received": row["paid_amount"], "by": row["payee_pt"]})
    return full(pengajuan_id)


TWIN = f"""FROM {P}.pengajuan a JOIN {P}.pengajuan b ON b.id <> a.id AND b.requested_amount = a.requested_amount
              AND regexp_replace(b.payee_account, '[^0-9A-Za-z]', '', 'g') = regexp_replace(a.payee_account, '[^0-9A-Za-z]', '', 'g')
              AND abs(extract(epoch FROM b.first_submitted_at - a.first_submitted_at)) < 30 * 86400
           WHERE a.status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED') AND b.status NOT IN ('DRAFT', 'REJECTED', 'CANCELLED')
             AND a.requested_amount > 0 AND coalesce(a.payee_account, '') <> '' AND a.kind <> 'PETTY_CASH'"""


def similar(row: dict) -> list[str]:
    """Requests within 30 days of this one to the same account with the same amount (a warning for the approvers)."""
    if not (row.get("payee_account") or "").strip() or row["kind"] == "PETTY_CASH":
        return []
    return [r["number"] for r in db.fetch(f"SELECT DISTINCT b.number {TWIN} AND a.id::text = %s", (row["id"],))]


def twin_ids() -> set[str]:
    return {r["id"] for r in db.fetch(f"SELECT DISTINCT a.id::text AS id {TWIN}")}


def _option_id(v: Any, list_: str, label: str) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        oid = int(v)
    except (TypeError, ValueError):
        raise PengajuanError(f"{label} tidak dikenal") from None
    if not db.fetchrow(f"SELECT 1 AS x FROM {P}.fund_option WHERE id = %s AND list = %s", (oid, list_)):
        raise PengajuanError(f"{label} tidak dikenal")
    return oid


def personal_sets(user: dict, row: dict, body: dict) -> dict:
    """The personal fields of a draft; the identity is refreshed from the account at every save."""
    me = _profile(user)
    sets_unit = body.get("unit")
    if sets_unit:
        from app import fund_units
        allowed = fund_units.requests_for(user, branch_options(user))
        if sets_unit not in allowed or (row["kind"] != "PERSONAL" and not (fund_units.get(sets_unit) or {}).get("vendor")):
            raise PengajuanError("Unit di luar akses Anda")
    sets: dict[str, Any] = {**({"unit": sets_unit} if sets_unit else {}), "email": me["email"], "department": me["department"], "job_title": me["jobTitle"], "requester_name": me["name"]}
    for key, list_, label, col in (("purposeId", "PURPOSE", "Keperluan Dana", "purpose_id"),
                                   ("transactionTypeId", "TRANSACTION", "Jenis Transaksi", "transaction_type_id"),
                                   ("projectId", "PROJECT", "Project", "project_id"), ("accountTypeId", "ACCOUNT", "Jenis Rekening", "account_type_id")):
        if key in body:
            sets[col] = _option_id(body[key], list_, label)
    if "requestedAmount" in body:
        sets["requested_amount"] = _amount(body["requestedAmount"], "Total dana diajukan")
    if "branchCode" in body:
        code = (body["branchCode"] or "").strip()
        if code:
            b = next((o for o in (*branch_options(user), *outlet_options()) if o["code"] == code), None)
            if b is None:
                raise PengajuanError("Outlet tidak dikenal")
            sets["branch_code"], sets["branch_name"] = b["code"], b["name"]
        else:
            sets["branch_code"] = sets["branch_name"] = None
    if "settlesId" in body:
        sid = (body["settlesId"] or "").strip() or None
        if sid and sid not in {a["id"] for a in open_advances(str(user["id"]), exclude=row["id"])}:
            raise PengajuanError("Cash in Advance tidak ditemukan atau sudah diselesaikan")
        sets["settles_id"] = sid
    return sets


def missing_personal(row: dict, files: list[dict]) -> list[str]:
    from app import fund_options
    out = []
    purpose = fund_options.get(row.get("purpose_id"))
    flags = (purpose or {}).get("settings") or {}
    if purpose is None:
        out.append("Pilih Keperluan Dana")
    if not row.get("transaction_type_id"):
        out.append("Pilih Jenis Transaksi")
    if not row.get("needed_on"):
        out.append("Isi tanggal dana dibutuhkan")
    if not (row.get("purpose") or "").strip():
        out.append("Tulis deskripsi dan detail pengajuan dana")
    if row.get("requested_amount") is None or row["requested_amount"] <= 0:
        out.append("Isi total dana diajukan")
    if flags.get("outlet") and not row.get("branch_code"):
        out.append("Pilih outlet")
    if not row.get("account_type_id"):
        out.append("Pilih jenis rekening tujuan")
    if row.get("payee_type") == "VA":
        if not (row.get("payee_account") or "").strip() or not ((row.get("va_provider") or row.get("payee_bank") or "").strip()):
            out.append("Isi Virtual Account tujuan: penyedia (mis. Shopee, Tokopedia) dan nomor VA")
    elif not (row.get("payee_bank") or "").strip() or not (row.get("payee_account") or "").strip() or not (row.get("payee_name") or "").strip():
        out.append("Isi rekening tujuan: bank, nomor rekening dan nama pemilik")
    if flags.get("type") == "SETTLEMENT" and not row.get("settles_id"):
        out.append("Pilih Cash in Advance yang diselesaikan")
    if flags.get("receipt") and not any(f["kind"] in ("OTHER", "LINE") for f in files):
        out.append("Lampirkan dokumen pendukung (nota / kwitansi / foto)")
    return out


def check_personal(row: dict) -> Optional[Decimal]:
    """The plafon of the department for a Keperluan Dana marked deptPlafon (refused above it); None = no limit."""
    from app import fund_options
    purpose = fund_options.get(row.get("purpose_id")) or {}
    if not (purpose.get("settings") or {}).get("deptPlafon"):
        return None
    plafon = fund_options.dept_plafon(row.get("department"))
    if plafon is not None and row["requested_amount"] is not None and row["requested_amount"] > plafon:
        raise PengajuanError(f"Melebihi plafon departemen {row.get('department')}: maksimal Rp {plafon:,.0f}".replace(",", "."))
    return plafon


# ---------------------------------------------------------------- names of the outlet kinds (2026-11-20, migration 127)

def kind_labels() -> dict[str, str]:
    """The names management gave the outlet kinds (none = the default name of the app)."""
    try:
        return {r["kind"]: r["label"] for r in db.fetch(f"SELECT kind, label FROM {P}.fund_kind_label")}
    except Exception:  # noqa: BLE001 - before migration 127
        return {}


def list_names() -> dict[str, str]:
    """The titles of the lists of the personal form (Keperluan Dana …), as management named them."""
    from app import fund_options
    names = kind_labels()
    return {k: names.get(f"LIST:{k}") or v for k, v in fund_options.LISTS.items()}


def set_kind_label(user: dict, kind: str, label: Any) -> dict[str, str]:
    """The name of an outlet kind, or (LIST:<list>) the title of a list of the personal form."""
    from app import fund_options
    if kind not in OUTLET_KINDS + DE_KINDS and kind not in {f"LIST:{k}" for k in fund_options.LISTS}:
        raise PengajuanError("Jenis pengajuan tidak dikenal")
    name = " ".join(str(label or "").split())[:60]
    if not name:
        db.fetchrow(f"DELETE FROM {P}.fund_kind_label WHERE kind = %s RETURNING kind", (kind,))
    else:
        db.fetchrow(f"""INSERT INTO {P}.fund_kind_label (kind, label, updated_by) VALUES (%s, %s, %s)
                        ON CONFLICT (kind) DO UPDATE SET label = EXCLUDED.label, updated_at = now(), updated_by = EXCLUDED.updated_by
                        RETURNING kind""", (kind, name, _actor(user)))
    return kind_labels()
