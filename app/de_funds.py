"""The money of the Maintenance & Procurement requests goes through Fund Request (2026-11-21, migration 130).

Fund Request is the only way the money goes out (the user's decision). The team asks Finance for the money of a Damaged
Equipment request from its cost lines — nothing is typed twice: a pengajuan of kind MAINTENANCE (a repair) or PROCUREMENT
linked to the request (de_request_id), its lines the estimated cost lines, the payee the vendor. It follows the approval
flow management sets for its kind in Configurations, then Finance pays it.

  * gate(): a request with an estimated cost does not start (In progress / Ordered) before its fund is approved.
  * a REVISION from Finance: the team corrects the cost lines and asks again; the same pengajuan is sent again and its
    approvals start over. A REJECTED fund: the team asks a new one.
  * settle() at Done: a paid fund goes to Verifikasi with what its cost lines really cost (the technicians' labour is not
    part of the fund); Finance settles the difference in the same request (app/fund_lifecycle.py, migration 144).
Every step of the fund is an event on the request's timeline too.
"""
from decimal import Decimal
from typing import Any, Optional

from app import database as db
from app import fund_units
from app import pengajuan as pg
from app.damaged_equipment import P, DeError, _event as de_event, cost_items, get_row, media_root

FUND_KIND = {"REPAIR": "MAINTENANCE", "PROCUREMENT": "PROCUREMENT"}
CATEGORY = {"MATERIAL": "MATERIAL", "SERVICE": "SERVICE", "TRANSPORT": "TRANSPORT", "OTHER": "OTHER"}
ASK_FROM = ("ACCEPTED", "IN_PROGRESS", "ORDERED", "ON_HOLD")  # after the team accepted the request, before the handover
GATED = ("IN_PROGRESS", "ORDERED")
CLEARED = ("APPROVED", "PAID", "VERIFYING", "DONE")


def _money(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def fund_of(de_id: str) -> Optional[dict]:
    """The request's fund (not a settlement top-up): the open or paid one, else the latest rejected."""
    return db.fetchrow(f"""SELECT {pg.COLS}, p.refund_amount, p.refunded_at FROM {P}.pengajuan p
                           WHERE p.de_request_id::text = %s AND p.settles_id IS NULL AND p.kind IN ('MAINTENANCE', 'PROCUREMENT')
                           ORDER BY (p.status IN ('REJECTED', 'CANCELLED')), p.created_at DESC LIMIT 1""", (de_id,))


def needs_fund(row: dict) -> bool:
    return _money(row.get("est_cost")) > 0


def gate(row: dict, to: str) -> None:
    """Refuses to start the work of a request with a cost before Finance approved its fund."""
    if to not in GATED or not needs_fund(row):
        return
    fund = fund_of(row["id"])
    if fund is None or fund["status"] in ("REJECTED", "CANCELLED"):
        raise DeError("Ajukan dana ke Finance dulu (Biaya & invoice → Ajukan dana); pekerjaan dimulai setelah disetujui")
    if fund["status"] not in CLEARED:
        raise DeError(f"Dana {fund['number']} masih {pg.STATUS_LABEL[fund['status']].lower()}: tunggu disetujui Finance")


def lines_from(costs: list[dict]) -> list[dict]:
    """The fund's lines: the estimated cost lines of the request (a line without an estimate is left out)."""
    out = []
    for c in costs:
        amount = _money(c.get("estAmount"))
        if amount <= 0:
            continue
        qty = c.get("qty") or 1
        out.append({"category": f"TRX:{c['transactionId']}" if c.get("transactionId") else CATEGORY.get(c.get("kind"), "OTHER"), "amount": amount,
                    "description": f"{c['description']} (×{qty:g})" if qty != 1 else c["description"]})
    return out


def payee_of(payee: dict) -> tuple[str, str, str, str]:
    """(bank / VA provider, account / VA number, name, BANK | VA): a Virtual Account (Shopee, Tokopedia …, migration 144)
    needs its provider and number; a bank account its bank, number and owner."""
    ptype = "VA" if payee.get("type") == "VA" else "BANK"
    bank, account, name = (str(payee.get(k) or "").strip()[:120] for k in ("bank", "account", "name"))
    if ptype == "VA":
        if not (bank and account):
            raise DeError("Isi Virtual Account tujuan: penyedia (mis. Shopee, Tokopedia) dan nomor VA")
        return bank, account, name or bank, ptype
    if not (bank and account and name):
        raise DeError("Isi rekening tujuan: bank, nomor rekening dan nama pemilik")
    return bank, account, name, ptype


def request_funds(user: dict, de_id: str, payee: dict, note: Optional[str]) -> dict:
    """Ask Finance for the money of the request (again after a revision). Returns the request for the team."""
    from app import damaged_equipment as de
    row = get_row(de_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if row["status"] not in ASK_FROM:
        raise DeError("Dana diajukan setelah request disetujui tim dan sebelum serah terima")
    lines = lines_from(cost_items(de_id))
    if not lines:
        raise DeError("Isi estimasi biaya dulu (Biaya & invoice): dana diajukan dari baris biaya")
    bank, account, name, ptype = payee_of(payee)
    fund = fund_of(de_id)
    if fund is not None and fund["status"] not in ("REVISION", "REJECTED", "CANCELLED"):
        raise DeError(f"Dana sudah diajukan ({fund['number']}, {pg.STATUS_LABEL[fund['status']].lower()})")
    revise = fund is not None and fund["status"] == "REVISION"
    total = sum((ln["amount"] for ln in lines), Decimal(0))
    kind = FUND_KIND[row["kind"]]
    purpose = f"{row['number']} · {de.KINDS[row['kind']]} {row['branch_name']}"
    with db.transaction() as conn:
        if revise:
            fund_id, number = fund["id"], fund["number"]
            conn.execute(f"DELETE FROM {P}.pengajuan_line WHERE pengajuan_id::text = %s", (fund_id,))
        else:
            number = pg._next_number(conn)
            fund_id = conn.execute(
                f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, branch_code, branch_name, kind, de_request_id,
                        requester_name, number, first_submitted_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) RETURNING id::text""",
                (user["id"], user["username"], user.get("full_name"), row["branch_code"], row["branch_name"], kind, de_id,
                 user.get("full_name") or user["username"], number)).fetchone()["id"]
        for i, ln in enumerate(lines):
            conn.execute(f"""INSERT INTO {P}.pengajuan_line (pengajuan_id, sort, category, description, amount)
                             VALUES (%s, %s, %s, %s, %s)""", (fund_id, i, ln["category"], ln["description"], ln["amount"]))
        conn.execute(f"""UPDATE {P}.pengajuan SET status = 'SUBMITTED', approval_step = 1, submitted_at = now(), purpose = %s,
                             payee_bank = %s, payee_account = %s, payee_name = %s, payee_type = %s, va_provider = %s, note = %s,
                             total_out = %s, requested_amount = %s, approved_at = NULL, approved_by = NULL, approved_amount = NULL,
                             updated_at = now()
                         WHERE id::text = %s""",
                     (purpose, bank, account, name, ptype, bank if ptype == "VA" else None, (note or "").strip()[:2000] or None,
                      total, total, fund_id))
        fund_units.stamp(conn, fund_id, fund_units.unit_of_branch(row["branch_code"]))
        pg._event(conn, fund_id, user, "SUBMIT", "REVISION" if revise else "DRAFT", "SUBMITTED", None,
                  {"requested": total, "lines": len(lines), "resubmit": revise, "deRequest": row["number"]})
        de_event(conn, de_id, user, "FUND", None, "SUBMITTED", note,
                 {"fund": number, "amount": float(total), "resubmit": revise})
    attach_quotes(user, de_id, fund_id)
    return de.team_full(de_id)


def attach_quotes(user: dict, de_id: str, fund_id: str, kind: str = "OTHER") -> int:
    """The estimates / invoices of the request copied to the fund (Finance may not open the request's page); a file
    already on the fund (same content) is skipped. Copies, never shared paths: deleting one never deletes the other."""
    have = {r["sha256"] for r in db.fetch(f"SELECT sha256 FROM {P}.pengajuan_file WHERE pengajuan_id::text = %s AND kind = %s",
                                         (fund_id, kind))}
    rows = db.fetch(f"""SELECT storage_path, sha256, file_name FROM {P}.de_media WHERE request_id::text = %s AND kind = 'INVOICE'
                        ORDER BY uploaded_at""", (de_id,))
    added = 0
    for r in rows:
        path = media_root() / r["storage_path"]
        if r["sha256"] in have or not path.is_file():
            continue
        pg.add_file(user, fund_id, kind, path.read_bytes(), r["file_name"] or "invoice", "INVOICE", None)
        have.add(r["sha256"])
        added += 1
    return added


def on_fund_status(conn, fund: dict, user: dict, to: str, note: Optional[str], details: dict) -> None:
    """A step of the fund in Fund Request, on the request's timeline (called inside the fund's transaction)."""
    if fund.get("de_request_id"):
        de_event(conn, fund["de_request_id"], user, "FUND", fund["status"], to, note,
                 {"fund": fund["number"], **{k: v for k, v in details.items() if k in ("approvedAmount", "amount", "step")}})


def settlement(fund_amount: Decimal, actual: Decimal, paid: bool) -> tuple[str, Decimal]:
    """("TOP_UP" | "REFUND" | "UNPAID_LESS" | "EVEN", amount): what the difference of the actual cost asks for."""
    diff = actual - fund_amount
    if diff > 0:
        return "TOP_UP", diff
    if diff < 0:
        return ("REFUND" if paid else "UNPAID_LESS"), -diff
    return "EVEN", Decimal(0)


def spent_on_lines(de_id: str) -> Optional[Decimal]:
    """What the cost lines really cost: the fund pays the lines, never the labour. None while no line has its actual."""
    vals = [c["actualAmount"] for c in cost_items(de_id) if c["actualAmount"] is not None]
    return sum((_money(v) for v in vals), Decimal(0)) if vals else None


def settle(conn, row: dict, user: dict) -> Optional[dict]:
    """At Done (2026-11-24, migration 144): the request is finished — its handover done, so the goods received / the work
    done — and its invoices are on it: a fund already paid (In Progress) goes to Verifikasi with the actual cost as the amount
    spent; Finance checks it and settles the difference in the same request (money back, or the shortfall paid). A fund not
    paid yet only gets a note: Finance pays, then the proof follows. Called inside the Done transaction."""
    fund = conn.execute(f"""SELECT id::text, number, status, paid_amount, topup_amount FROM {P}.pengajuan
                            WHERE de_request_id::text = %s AND settles_id IS NULL AND kind IN ('MAINTENANCE', 'PROCUREMENT')
                              AND status IN ('APPROVED', 'PAID')""", (row["id"],)).fetchone()
    if fund is None:
        return None
    actual = spent_on_lines(row["id"])  # None: Finance sets the accepted amount when verifying
    num = float(actual) if actual is not None else None
    if fund["status"] == "APPROVED":
        pg._event(conn, fund["id"], user, "NOTE", None, None,
                  f"{row['number']} selesai: setelah dana dibayar, bukti dikirim untuk verifikasi", {"actual": num})
        return {"kind": "UNPAID", "amount": num}
    from app import fund_lifecycle
    diff = fund_lifecycle.difference(fund["paid_amount"], fund["topup_amount"], actual) if actual is not None else None
    conn.execute(f"""UPDATE {P}.pengajuan SET status = 'VERIFYING', actual_amount = %s, actual_note = %s, proof_submitted_at = now(),
                         updated_at = now() WHERE id::text = %s""", (actual, f"Biaya aktual {row['number']} (selesai)", fund["id"]))
    pg._event(conn, fund["id"], user, "PROOF", "PAID", "VERIFYING", None, {"actual": actual, "difference": diff, "deRequest": row["number"]})
    de_event(conn, row["id"], user, "FUND", "PAID", "VERIFYING", None, {"fund": fund["number"], "actual": num})
    return {"kind": "VERIFYING", "amount": num, "fundId": fund["id"]}


def cancel_fund(user: dict, de_id: str, note: Optional[str]) -> dict:
    """The team withdraws the fund of the request before Finance pays it (2026-11-25); it may ask a new one afterwards."""
    from app import damaged_equipment as de
    fund = fund_of(de_id)
    if fund is None or fund["status"] not in pg.CANCELLABLE:
        raise DeError("Dana dibatalkan sebelum dicairkan Finance")
    note = (note or "").strip()[:2000]
    if not note:
        raise DeError("Tulis alasan pembatalan")
    with db.transaction() as conn:
        hit = conn.execute(f"""UPDATE {P}.pengajuan SET status = 'CANCELLED', closed_at = now(), updated_at = now()
                               WHERE id::text = %s AND status = ANY(%s) RETURNING id""", (fund["id"], list(pg.CANCELLABLE))).fetchone()
        if hit is None:
            raise DeError("Dana baru saja diproses; muat ulang")
        pg._event(conn, fund["id"], user, "STATUS", fund["status"], "CANCELLED", note, {"action": "CANCELLED", "byTeam": True})
        de_event(conn, de_id, user, "FUND", fund["status"], "CANCELLED", note, {"fund": fund["number"]})
    return de.team_full(de_id)


def send_proof(user: dict, de_id: str) -> dict:
    """The team sends the proof of a paid fund by hand (the request handed over / done, its invoices attached)."""
    from app import damaged_equipment as de
    from app import fund_lifecycle
    row = get_row(de_id)
    fund = fund_of(de_id)
    if row is None or fund is None or fund["status"] != "PAID":
        raise DeError("Bukti dikirim setelah dana dicairkan Finance")
    if row["status"] not in ("HANDOVER", "DONE"):
        raise DeError("Kirim bukti setelah serah terima: barang diterima / pekerjaan selesai")
    actual = spent_on_lines(de_id)
    if actual is None:
        raise DeError("Isi harga aktual di baris biaya dulu (Biaya & invoice)")
    attach_quotes(user, de_id, fund["id"], kind="PROOF")
    fund_lifecycle.submit_proof(user, fund["id"], actual,
                                f"Biaya aktual {row['number']}", by_team=True)
    return de.team_full(de_id)


# ---------------------------------------------------------------- technicians' Cash in Advance (migration 143)

def advance_purpose() -> Optional[int]:
    """The Keperluan Dana of type ADVANCE (Deposit / Cash in Advance) management keeps active."""
    r = db.fetchrow(f"""SELECT id FROM {P}.fund_option WHERE list = 'PURPOSE' AND is_active AND settings->>'type' = 'ADVANCE'
                        ORDER BY sort, id LIMIT 1""")
    return r["id"] if r else None


def advances_of(de_id: str) -> list[dict]:
    """The technicians' advances of a request, each with its settlement (Laporan Penyelesaian) when there is one."""
    rows = db.fetch(f"""SELECT p.id::text, p.user_id::text, p.number, p.status, p.requester_name, p.requested_amount, p.approved_amount, p.paid_amount,
                               p.created_at, s.number AS settle_number, s.status AS settle_status, s.requested_amount AS settle_amount
                        FROM {P}.pengajuan p
                        LEFT JOIN LATERAL (SELECT number, status, requested_amount FROM {P}.pengajuan x
                                           WHERE x.settles_id = p.id AND x.status NOT IN ('REJECTED', 'CANCELLED') ORDER BY x.created_at DESC LIMIT 1) s ON true
                        WHERE p.de_request_id::text = %s AND p.kind = 'PERSONAL' ORDER BY p.created_at""", (de_id,))
    return [{"id": r["id"], "userId": r["user_id"], "number": r["number"], "status": r["status"], "statusLabel": pg.STATUS_LABEL[r["status"]],
             "technician": r["requester_name"], "amount": pg._num(r["requested_amount"]), "approvedAmount": pg._num(r["approved_amount"]),
             "paidAmount": pg._num(r["paid_amount"]), "settlement": {"number": r["settle_number"], "status": r["settle_status"],
             "statusLabel": pg.STATUS_LABEL[r["settle_status"]], "amount": pg._num(r["settle_amount"])} if r["settle_number"] else None}
            for r in rows]


def request_advance(user: dict, de_id: str, body: dict) -> dict:
    """The team asks a Cash in Advance for a technician of the request: a personal Fund Request of the ADVANCE Keperluan
    Dana (its own approval flow), owned by the asking team member (who settles it later with the receipts), on behalf of
    the technician, paid to the technician's account (kept for the next time)."""
    from app import damaged_equipment as de
    row = get_row(de_id)
    if row is None or row["status"] == "DRAFT":
        raise LookupError("Pengajuan tidak ditemukan")
    if row["status"] not in ASK_FROM:
        raise DeError("Uang muka teknisi diajukan setelah request disetujui tim dan sebelum serah terima")
    tech = db.fetchrow(f"""SELECT t.id, t.name FROM {P}.de_technician t JOIN {P}.de_assignment a ON a.technician_id = t.id
                           WHERE t.id = %s AND a.request_id::text = %s AND a.status IN ('ACTIVE', 'DONE') LIMIT 1""",
                       (int(body.get("technicianId") or 0), de_id))
    if tech is None:
        raise DeError("Pilih teknisi yang ditugaskan di request ini")
    amount = _money(body.get("amount"))
    if amount <= 0 or amount > Decimal("1000000000"):
        raise DeError("Isi nominal uang muka")
    purpose_id = advance_purpose()
    if purpose_id is None:
        raise DeError("Keperluan Dana 'Deposit / Cash in Advance' tidak aktif di Pengaturan Fund Request")
    trx = db.fetchrow(f"SELECT id FROM {P}.fund_option WHERE id = %s AND list = 'TRANSACTION' AND is_active", (int(body.get("transactionId") or 0),))
    if trx is None:
        raise DeError("Pilih Jenis Transaksi")
    acct = db.fetchrow(f"SELECT id FROM {P}.fund_option WHERE id = %s AND list = 'ACCOUNT' AND is_active", (int(body.get("accountTypeId") or 0),))
    if acct is None:
        raise DeError("Pilih jenis rekening tujuan")
    bank, account, name = (str(body.get(k) or "").strip()[:120] for k in ("bank", "account", "name"))
    if not (bank and account and name):
        raise DeError("Isi rekening teknisi: bank, nomor rekening dan nama pemilik")
    note = (str(body.get("note") or "")).strip()[:2000]
    me = pg._profile(user)
    text = f"Uang muka teknisi {tech['name']} · {row['number']} {de.KINDS[row['kind']]} {row['branch_name']}" + (f"\n{note}" if note else "")
    with db.transaction() as conn:
        number = pg._next_number(conn)
        pid = conn.execute(
            f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, kind, baristas, email, department, job_title, requester_name,
                    branch_code, branch_name, purpose_id, transaction_type_id, account_type_id, needed_on, purpose, requested_amount,
                    payee_bank, payee_account, payee_name, de_request_id, number, status, approval_step, submitted_at, first_submitted_at)
                VALUES (%s, %s, %s, 'PERSONAL', '[]'::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, current_date, %s, %s, %s, %s, %s, %s, %s,
                        'SUBMITTED', 1, now(), now()) RETURNING id::text""",
            (user["id"], user["username"], user.get("full_name"), me["email"], me["department"], me["jobTitle"], tech["name"],
             row["branch_code"], row["branch_name"], purpose_id, trx["id"], acct["id"], text, amount, bank, account, name, de_id, number)
        ).fetchone()["id"]
        fund_units.stamp(conn, pid, fund_units.unit_of_branch(row["branch_code"]))
        pg._event(conn, pid, user, "SUBMIT", "DRAFT", "SUBMITTED", None,
                  {"requested": amount, "deRequest": row["number"], "technician": tech["name"], "advance": True})
        conn.execute(f"UPDATE {P}.de_technician SET bank = %s, account_no = %s, account_name = %s, updated_at = now() WHERE id = %s",
                     (bank, account, name, tech["id"]))
        de_event(conn, de_id, user, "FUND", None, "ADVANCE", note or None,
                 {"fund": number, "amount": float(amount), "technician": tech["name"]})
    return de.team_full(de_id)


def view_of(de_id: str) -> Optional[dict]:
    """The fund card of the request: its fund and the settlement top-ups."""
    fund = fund_of(de_id)
    if fund is None:
        return None
    tops = db.fetch(f"""SELECT id::text, number, status, requested_amount, approved_amount, paid_amount FROM {P}.pengajuan
                        WHERE settles_id::text = %s ORDER BY created_at""", (fund["id"],))
    v = pg.view(fund)
    return {**{k: v[k] for k in ("id", "number", "status", "statusLabel", "requestedAmount", "approvedAmount", "paidAmount",
                                "paidOn", "payeeBank", "payeeAccount", "payeeName", "approvalStep")},
            "step": (pg.current_step(fund) or {}).get("label"), "refundAmount": pg._num(fund["refund_amount"]),
            "refundedAt": pg._iso(fund["refunded_at"]),
            "topUps": [{"id": t["id"], "number": t["number"], "status": t["status"], "statusLabel": pg.STATUS_LABEL[t["status"]],
                        "amount": pg._num(t["requested_amount"])} for t in tops]}
