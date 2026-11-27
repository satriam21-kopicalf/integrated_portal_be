"""The whole life of a Fund Request after the money goes out (migration 144, 2026-11-24, user decision).

    Pending (SUBMITTED) → Approved → In Progress (PAID: money out, the proof awaited) → Verifikasi (VERIFYING) → Done

The rules of every kind are Finance's (fund_rule, key = the outlet kind, MAINTENANCE / PROCUREMENT or PERSONAL:<Keperluan
Dana id>): whether the proof comes after the money (else Paid is Done at once), within how many days, which documents,
and whether it is a vendor payment (due date, invoice number and invoice required when sent). The requester attaches the
proof and the actual amount in the same request; Finance checks it, may adjust the accepted amount (with a note), sends
it back when proof is missing, and closes it once the difference is settled: less spent → the money back (confirmed with
its proof), more → Finance pays the shortfall in the same request. A setting may refuse new requests of someone whose
proof is overdue (off by default).
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from app import database as db
from app.pic_checkin import CheckInError

P = "integration_portal"
DOCS = ("NOTA", "KWITANSI", "INVOICE", "STRUK", "TRANSFER", "FOTO", "QUOTE", "OTHER")
DEFAULT_RULE = {"proofAfter": False, "proofDays": 7, "proofDocs": [], "vendor": False}


class LifecycleError(CheckInError):  # a 422 in the routes, like PengajuanError
    pass


def _money(v: Any) -> Decimal:
    return Decimal(str(v or 0))


# ---------------------------------------------------------------- rules & settings

def rule_key(row: dict) -> str:
    return f"PERSONAL:{row.get('purpose_id')}" if row.get("kind") == "PERSONAL" else str(row.get("kind"))


def _rule_view(r: Optional[dict]) -> dict:
    if not r:
        return dict(DEFAULT_RULE)
    return {"proofAfter": r["proof_after"], "proofDays": r["proof_days"], "proofDocs": list(r["proof_docs"] or []), "vendor": r["vendor"]}


def rule_for(row: dict) -> dict:
    r = db.fetchrow(f"SELECT * FROM {P}.fund_rule WHERE key = %s", (rule_key(row),))
    if r is None and row.get("kind") == "PERSONAL":  # a Keperluan Dana made later: a Cash in Advance always owes its proof
        from app import fund_options
        o = fund_options.get(row.get("purpose_id")) or {}
        return {**DEFAULT_RULE, "proofAfter": (o.get("settings") or {}).get("type") == "ADVANCE"}
    return _rule_view(r)


def rules() -> list[dict]:
    """Every kind with its rule (Finance's settings page): the outlet kinds, Maintenance / Procurement, each Keperluan Dana."""
    from app import pengajuan as pg
    have = {r["key"]: r for r in db.fetch(f"SELECT * FROM {P}.fund_rule")}
    labels = pg.kind_labels()
    keys = [(k, labels.get(k) or pg.KINDS[k], "outlet") for k in pg.OUTLET_KINDS] + \
           [(k, pg.KINDS[k], "de") for k in pg.TEAM_KINDS] + \
           [("VENDOR", pg.KINDS["VENDOR"], "outlet")] + \
           [(f"PERSONAL:{o['id']}", o["name"], "personal") for o in db.fetch(
               f"SELECT id, name FROM {P}.fund_option WHERE list = 'PURPOSE' AND is_active ORDER BY sort, name")]
    def of(k: str) -> dict:
        return rule_for({"kind": "PERSONAL", "purpose_id": int(k[9:])}) if k.startswith("PERSONAL:") and k not in have else _rule_view(have.get(k))
    return [{"key": k, "label": label, "group": group, **of(k)} for k, label, group in keys]


def set_rule(user: dict, key: str, body: dict) -> list[dict]:
    if not (key in ("PETTY_CASH", "EMERGENCY", "MARKETING", "EVENT", "BILL", "MAINTENANCE", "PROCUREMENT", "VENDOR", "BUILDING")
            or (key.startswith("PERSONAL:") and key[9:].isdigit())):
        raise LifecycleError("Jenis pengajuan tidak dikenal")
    days = int(body.get("proofDays") or 7)
    if not 1 <= days <= 180:
        raise LifecycleError("Batas hari bukti 1–180")
    docs = [d for d in (body.get("proofDocs") or []) if d in DOCS]
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.fund_rule (key, proof_after, proof_days, proof_docs, vendor, updated_by)
                         VALUES (%s, %s, %s, %s, %s, %s)
                         ON CONFLICT (key) DO UPDATE SET proof_after = EXCLUDED.proof_after, proof_days = EXCLUDED.proof_days,
                           proof_docs = EXCLUDED.proof_docs, vendor = EXCLUDED.vendor, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                     (key, bool(body.get("proofAfter")), days, docs, bool(body.get("vendor")), user.get("full_name") or user["username"]))
    return rules()


def settings() -> dict:
    s = {r["key"]: r["value"] for r in db.fetch(f"SELECT key, value FROM {P}.fund_setting")}
    return {"blockOverdueProof": s.get("block_overdue_proof") == "true", "minLeadDays": int(s.get("min_lead_days") or 3)}


def set_settings(body: dict) -> dict:
    with db.transaction() as conn:
        if "blockOverdueProof" in body:
            conn.execute(f"""INSERT INTO {P}.fund_setting (key, value) VALUES ('block_overdue_proof', %s)
                             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", ("true" if body["blockOverdueProof"] else "false",))
        if "minLeadDays" in body:
            n = int(body["minLeadDays"])
            if not 0 <= n <= 60:
                raise LifecycleError("Minimal hari jatuh tempo 0–60")
            conn.execute(f"""INSERT INTO {P}.fund_setting (key, value) VALUES ('min_lead_days', %s)
                             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (str(n),))
    return settings()


# ---------------------------------------------------------------- pure rules (tested)

def after_paid(rule: dict, paid_on: date) -> tuple[str, Optional[date]]:
    """The status a request takes once paid, and the day its proof is due."""
    if rule.get("proofAfter"):
        return "PAID", paid_on + timedelta(days=int(rule.get("proofDays") or 7))
    return "DONE", None


def difference(paid: Any, topup: Any, accepted: Any) -> Decimal:
    """accepted − what went out (paid + the shortfall paid on top): > 0 Finance still owes, < 0 money to come back."""
    return _money(accepted) - (_money(paid) + _money(topup))


def outstanding(paid: Any, topup: Any, refunded: Any, accepted: Any) -> Decimal:
    """What is still to settle: accepted − (paid + shortfall paid − money already back)."""
    return _money(accepted) - (_money(paid) + _money(topup) - _money(refunded))


def _refunded(row: dict) -> Any:
    return row.get("refund_amount") if row.get("refunded_at") else None


def missing_proof(rule: dict, files: list[dict], actual: Any) -> list[str]:
    out = []
    proof = [f for f in files if f["kind"] == "PROOF"]
    if not proof:
        out.append("Lampirkan bukti (nota / invoice / foto barang diterima)")
    for d in rule.get("proofDocs") or []:
        if proof and not any(f.get("docType") == d for f in proof):
            out.append(f"Lampirkan {DOC_LABEL.get(d, d)}")
    if actual is None:
        out.append("Isi nominal yang benar-benar terpakai")
    return out


DOC_LABEL = {"NOTA": "nota", "KWITANSI": "kwitansi", "INVOICE": "invoice", "STRUK": "struk", "TRANSFER": "bukti transfer",
             "FOTO": "foto barang diterima", "QUOTE": "penawaran harga", "OTHER": "dokumen lainnya"}


def vendor_problems(rule: dict, needed_on: Optional[date], invoice_no: Optional[str], files: list[dict], today: date,
                    min_lead: int) -> tuple[list[str], list[str]]:
    """(errors, warnings) of a vendor payment when sent: due date not past, invoice number and invoice file."""
    if not rule.get("vendor"):
        return [], []
    errors, warns = [], []
    if needed_on is None:
        errors.append("Isi tanggal jatuh tempo")
    elif needed_on < today:
        errors.append("Jatuh tempo sudah lewat: konfirmasi dengan vendor dan isi tanggal yang berlaku")
    elif (needed_on - today).days < min_lead:
        warns.append(f"Jatuh tempo kurang dari {min_lead} hari: Finance butuh waktu proses")
    if not (invoice_no or "").strip():
        errors.append("Isi nomor invoice vendor")
    if not any(f.get("docType") == "INVOICE" for f in files):
        errors.append("Lampirkan file invoice vendor")
    return errors, warns


# ---------------------------------------------------------------- actions

def overdue_of(user_id: str) -> list[str]:
    rows = db.fetch(f"""SELECT number FROM {P}.pengajuan WHERE user_id::text = %s AND status = 'PAID'
                        AND proof_due_on IS NOT NULL AND proof_due_on < current_date ORDER BY proof_due_on""", (user_id,))
    return [r["number"] for r in rows]


def check_send(row: dict, files: list[dict], today: date) -> list[str]:
    """Refusals when a request is sent: a vendor payment's due date / invoice, an overdue proof (when Finance blocks)."""
    st = settings()
    errors, _ = vendor_problems(rule_for(row), row.get("needed_on"), row.get("invoice_no"), files, today, st["minLeadDays"])
    if st["blockOverdueProof"]:
        late = overdue_of(row["user_id"])
        if late:
            errors.append(f"Bukti {', '.join(late[:3])} sudah lewat batas: lengkapi dulu sebelum mengajukan dana baru")
    return errors


def submit_proof(user: dict, pengajuan_id: str, actual: Any, note: Optional[str], by_team: bool = False) -> dict:
    """The requester (or the Maintenance & Procurement team for its fund) sends the proof: In Progress → Verifikasi."""
    from app import pengajuan as pg
    row = pg.get_row(pengajuan_id)
    if row is None or (not by_team and row["user_id"] != str(user["id"])):
        raise LookupError("Pengajuan tidak ditemukan")
    if row["kind"] == "INTERCOMPANY":
        raise LifecycleError("Antar-PT selesai setelah PT penerima mengonfirmasi uang masuk")
    if row["status"] != "PAID":
        raise LifecycleError("Bukti dikirim saat dana sudah cair dan belum diverifikasi")
    if row["kind"] in pg.DE_KINDS and not by_team:
        raise LifecycleError("Bukti dana Maintenance & Procurement dikirim dari request-nya, setelah barang diterima / pekerjaan selesai")
    amount = None if actual in (None, "") else _money(actual)
    if amount is not None and (amount < 0 or amount > Decimal("1000000000000")):
        raise LifecycleError("Nominal terpakai tidak valid")
    # the team's fund: the goods received / work done is the handover of its request, the files asked are its invoices
    todo = missing_proof({} if by_team else rule_for(row), pg.files_of(pengajuan_id), amount)
    if todo:
        raise LifecycleError("Belum bisa dikirim: " + "; ".join(todo))
    note = (note or "").strip()[:2000] or None
    diff = difference(row["paid_amount"], row.get("topup_amount"), amount)
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.pengajuan SET status = 'VERIFYING', actual_amount = %s, actual_note = %s, proof_submitted_at = now(),
                             verified_amount = NULL, verified_note = NULL, updated_at = now() WHERE id::text = %s""",
                     (amount, note, pengajuan_id))
        pg._event(conn, pengajuan_id, user, "PROOF", "PAID", "VERIFYING", note, {"actual": amount, "difference": diff})
        from app import de_funds
        de_funds.on_fund_status(conn, row, user, "VERIFYING", note, {"amount": amount})
    return pg.full(pengajuan_id)


def verify(user: dict, pengajuan_id: str, action: str, amount: Any, note: Optional[str]) -> dict:
    """Finance on a request in Verifikasi: ADJUST (the accepted amount, note required), MISSING (back to In Progress, note
    required), TOPUP (pay the shortfall: amount + transfer proof), DONE (once the difference is settled)."""
    from app import pengajuan as pg
    row = pg.get_row(pengajuan_id)
    if row is None or row["status"] != "VERIFYING":
        raise LookupError("Pengajuan tidak dalam verifikasi")
    if not pg.takes(user, pg._pay(pg.steps_of(row))):
        raise PermissionError("Verifikasi bukti oleh tahap pembayaran Finance")
    note = (note or "").strip()[:2000] or None
    accepted = row["verified_amount"] if row.get("verified_amount") is not None else row.get("actual_amount")
    diff = outstanding(row["paid_amount"], row.get("topup_amount"), _refunded(row), accepted)
    actor = pg._actor(user)
    waiting_refund = False
    with db.transaction() as conn:
        if action == "ADJUST":
            value = _money(amount)
            if amount in (None, "") or value < 0:
                raise LifecycleError("Isi nominal yang diakui Finance")
            if not note:
                raise LifecycleError("Tulis alasan penyesuaian")
            conn.execute(f"""UPDATE {P}.pengajuan SET verified_amount = %s, verified_note = %s, updated_at = now(),
                             refund_amount = CASE WHEN refunded_at IS NULL THEN NULL ELSE refund_amount END WHERE id::text = %s""",
                         (value, note, pengajuan_id))
            pg._event(conn, pengajuan_id, user, "VERIFY", "VERIFYING", "VERIFYING", note,
                      {"action": "ADJUST", "from": accepted, "to": value, "difference": difference(row["paid_amount"], row.get("topup_amount"), value)})
        elif action == "MISSING":
            if not note:
                raise LifecycleError("Tulis bukti apa yang masih kurang")
            conn.execute(f"UPDATE {P}.pengajuan SET status = 'PAID', updated_at = now() WHERE id::text = %s", (pengajuan_id,))
            pg._event(conn, pengajuan_id, user, "VERIFY", "VERIFYING", "PAID", note, {"action": "MISSING"})
        elif action == "TOPUP":
            value = _money(amount)
            if diff <= 0:
                raise LifecycleError("Tidak ada kekurangan yang perlu dibayar")
            if value <= 0 or value > diff:
                raise LifecycleError(f"Nominal kekurangan maksimal Rp {diff:,.0f}".replace(",", "."))
            if not any(f["kind"] == "TOPUP" for f in pg.files_of(pengajuan_id)):
                raise LifecycleError("Lampirkan bukti transfer kekurangan dulu")
            conn.execute(f"""UPDATE {P}.pengajuan SET topup_amount = coalesce(topup_amount, 0) + %s, topup_paid_at = now(), updated_at = now()
                             WHERE id::text = %s""", (value, pengajuan_id))
            pg._event(conn, pengajuan_id, user, "VERIFY", "VERIFYING", "VERIFYING", note, {"action": "TOPUP", "amount": value})
        elif action == "DONE":
            if accepted is None:
                raise LifecycleError("Nominal terpakai belum diisi")
            if diff > 0:
                raise LifecycleError(f"Masih ada kekurangan Rp {diff:,.0f}: bayar kekurangannya dulu".replace(",", "."))
            if diff < 0:
                if row.get("refunded_at"):
                    raise LifecycleError(f"Pengembalian masih kurang Rp {-diff:,.0f}: sesuaikan nominal yang diakui".replace(",", "."))
                if row.get("refund_amount") is None:
                    conn.execute(f"UPDATE {P}.pengajuan SET refund_amount = %s, updated_at = now() WHERE id::text = %s", (-diff, pengajuan_id))
                    pg._event(conn, pengajuan_id, user, "NOTE", None, None,
                              f"Sisa dana Rp {-diff:,.0f} dikembalikan; Done setelah bukti pengembalian dikonfirmasi".replace(",", "."), {"refund": -diff})
                    waiting_refund = True
                else:
                    raise LifecycleError("Konfirmasi dulu bukti pengembalian sisa dana")
            if not waiting_refund:
                _close(conn, row, user, actor, pengajuan_id, accepted, diff, note)
        else:
            raise LifecycleError("Langkah verifikasi tidak dikenal")
    return pg.full(pengajuan_id)


def _close(conn, row: dict, user: dict, actor: str, pengajuan_id: str, accepted: Any, diff: Decimal, note: Optional[str]) -> None:
    from app import de_funds
    from app import pengajuan as pg
    conn.execute(f"""UPDATE {P}.pengajuan SET status = 'DONE', done_at = now(), closed_at = now(), verified_at = now(), verified_by = %s,
                         verified_amount = coalesce(verified_amount, actual_amount), updated_at = now() WHERE id::text = %s""",
                 (actor, pengajuan_id))
    pg._event(conn, pengajuan_id, user, "STATUS", "VERIFYING", "DONE", note, {"accepted": accepted, "difference": diff})
    de_funds.on_fund_status(conn, row, user, "DONE", note, {"amount": accepted})


def view_fields(row: dict) -> dict:
    """The lifecycle of a request in its view."""
    accepted = row["verified_amount"] if row.get("verified_amount") is not None else row.get("actual_amount")
    due = row.get("proof_due_on")
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "proofDueOn": due.isoformat() if due else None,
        "proofOverdue": bool(due and row["status"] == "PAID" and due < date.today()),
        "proofSubmittedAt": row["proof_submitted_at"].isoformat() if row.get("proof_submitted_at") else None,
        "actualAmount": num(row.get("actual_amount")), "actualNote": row.get("actual_note"),
        "verifiedAmount": num(row.get("verified_amount")), "verifiedNote": row.get("verified_note"),
        "verifiedAt": row["verified_at"].isoformat() if row.get("verified_at") else None, "verifiedBy": row.get("verified_by"),
        "doneAt": row["done_at"].isoformat() if row.get("done_at") else None,
        "topupAmount": num(row.get("topup_amount")), "topupPaidAt": row["topup_paid_at"].isoformat() if row.get("topup_paid_at") else None,
        "difference": num(difference(row.get("paid_amount"), None, accepted)) if accepted is not None and row.get("paid_amount") is not None else None,
        "outstanding": num(outstanding(row.get("paid_amount"), row.get("topup_amount"), _refunded(row), accepted))
        if accepted is not None and row.get("paid_amount") is not None else None,
        "payeeType": row.get("payee_type") or "BANK", "vaProvider": row.get("va_provider"), "invoiceNo": row.get("invoice_no"),
    }
