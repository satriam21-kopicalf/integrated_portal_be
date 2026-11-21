"""The money of the Maintenance & Procurement requests goes through Fund Request (2026-11-21, migration 130).

Fund Request is the only way the money goes out (the user's decision). The team asks Finance for the money of a Damaged
Equipment request from its cost lines — nothing is typed twice: a pengajuan of kind MAINTENANCE (a repair) or PROCUREMENT
linked to the request (de_request_id), its lines the estimated cost lines, the payee the vendor. It follows the approval
flow management sets for its kind in Configurations, then Finance pays it.

  * gate(): a request with an estimated cost does not start (In progress / Ordered) before its fund is approved.
  * a REVISION from Finance: the team corrects the cost lines and asks again; the same pengajuan is sent again and its
    approvals start over. A REJECTED fund: the team asks a new one.
  * settle() at Done: actual cost − the fund (paid, else approved). More: a top-up pengajuan (settles_id → the fund)
    with the difference. Less, once paid: the fund gets refund_amount and Finance confirms the money back (REFUNDED).
Every step of the fund is an event on the request's timeline too.
"""
from decimal import Decimal
from typing import Any, Optional

from app import database as db
from app import pengajuan as pg
from app.damaged_equipment import P, DeError, _event as de_event, cost_items, get_row, media_root

FUND_KIND = {"REPAIR": "MAINTENANCE", "PROCUREMENT": "PROCUREMENT"}
CATEGORY = {"MATERIAL": "MATERIAL", "SERVICE": "SERVICE", "TRANSPORT": "TRANSPORT", "OTHER": "OTHER"}
ASK_FROM = ("ACCEPTED", "IN_PROGRESS", "ORDERED", "ON_HOLD")  # after the team accepted the request, before the handover
GATED = ("IN_PROGRESS", "ORDERED")
CLEARED = ("APPROVED", "PAID")


def _money(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def fund_of(de_id: str) -> Optional[dict]:
    """The request's fund (not a settlement top-up): the open or paid one, else the latest rejected."""
    return db.fetchrow(f"""SELECT {pg.COLS}, p.refund_amount, p.refunded_at FROM {P}.pengajuan p
                           WHERE p.de_request_id::text = %s AND p.settles_id IS NULL
                           ORDER BY (p.status = 'REJECTED'), p.created_at DESC LIMIT 1""", (de_id,))


def needs_fund(row: dict) -> bool:
    return _money(row.get("est_cost")) > 0


def gate(row: dict, to: str) -> None:
    """Refuses to start the work of a request with a cost before Finance approved its fund."""
    if to not in GATED or not needs_fund(row):
        return
    fund = fund_of(row["id"])
    if fund is None or fund["status"] == "REJECTED":
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
        out.append({"category": CATEGORY.get(c.get("kind"), "OTHER"), "amount": amount,
                    "description": f"{c['description']} (×{qty:g})" if qty != 1 else c["description"]})
    return out


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
    bank, account, name = (str(payee.get(k) or "").strip()[:120] for k in ("bank", "account", "name"))
    if not (bank and account and name):
        raise DeError("Isi rekening tujuan: bank, nomor rekening dan nama pemilik")
    fund = fund_of(de_id)
    if fund is not None and fund["status"] not in ("REVISION", "REJECTED"):
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
                             payee_bank = %s, payee_account = %s, payee_name = %s, note = %s, total_out = %s, requested_amount = %s,
                             approved_at = NULL, approved_by = NULL, approved_amount = NULL, updated_at = now()
                         WHERE id::text = %s""",
                     (purpose, bank, account, name, (note or "").strip()[:2000] or None, total, total, fund_id))
        pg._event(conn, fund_id, user, "SUBMIT", "REVISION" if revise else "DRAFT", "SUBMITTED", None,
                  {"requested": total, "lines": len(lines), "resubmit": revise, "deRequest": row["number"]})
        de_event(conn, de_id, user, "FUND", None, "SUBMITTED", note,
                 {"fund": number, "amount": float(total), "resubmit": revise})
    attach_quotes(user, de_id, fund_id)
    return de.team_full(de_id)


def attach_quotes(user: dict, de_id: str, fund_id: str) -> int:
    """The estimates / invoices of the request copied to the fund (Finance may not open the request's page); a file
    already on the fund (same content) is skipped. Copies, never shared paths: deleting one never deletes the other."""
    have = {r["sha256"] for r in db.fetch(f"SELECT sha256 FROM {P}.pengajuan_file WHERE pengajuan_id::text = %s", (fund_id,))}
    rows = db.fetch(f"""SELECT storage_path, sha256, file_name FROM {P}.de_media WHERE request_id::text = %s AND kind = 'INVOICE'
                        ORDER BY uploaded_at""", (de_id,))
    added = 0
    for r in rows:
        path = media_root() / r["storage_path"]
        if r["sha256"] in have or not path.is_file():
            continue
        pg.add_file(user, fund_id, "OTHER", path.read_bytes(), r["file_name"] or "invoice", "INVOICE", None)
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


def settle(conn, row: dict, user: dict) -> Optional[dict]:
    """At Done: the difference between the actual cost and the fund, once (called inside the Done transaction)."""
    fund = conn.execute(f"""SELECT id::text, number, status, kind, branch_code, branch_name, payee_bank, payee_account, payee_name,
                                   approved_amount, paid_amount, refund_amount, user_id, username, full_name
                              FROM {P}.pengajuan WHERE de_request_id::text = %s AND settles_id IS NULL AND status IN ('APPROVED', 'PAID')""",
                        (row["id"],)).fetchone()
    if fund is None or fund["refund_amount"] is not None or conn.execute(
            f"SELECT 1 FROM {P}.pengajuan WHERE settles_id::text = %s LIMIT 1", (fund["id"],)).fetchone():
        return None
    paid = fund["status"] == "PAID"
    base = _money(fund["paid_amount"] if paid else fund["approved_amount"])
    what, amount = settlement(base, _money(row.get("actual_cost")), paid)
    if what == "TOP_UP":
        number = pg._next_number(conn)
        top = conn.execute(
            f"""INSERT INTO {P}.pengajuan (user_id, username, full_name, branch_code, branch_name, kind, de_request_id, settles_id,
                    number, status, approval_step, submitted_at, first_submitted_at, purpose, requester_name, payee_bank,
                    payee_account, payee_name, total_out, requested_amount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'SUBMITTED', 1, now(), now(), %s, %s, %s, %s, %s, %s, %s)
                RETURNING id::text""",
            (fund["user_id"], fund["username"], fund["full_name"], fund["branch_code"], fund["branch_name"], fund["kind"], row["id"],
             fund["id"], number, f"Kekurangan biaya aktual {row['number']} (dana {fund['number']})", user.get("full_name") or user["username"],
             fund["payee_bank"], fund["payee_account"], fund["payee_name"], amount, amount)).fetchone()["id"]
        conn.execute(f"""INSERT INTO {P}.pengajuan_line (pengajuan_id, sort, category, description, amount)
                         VALUES (%s, 0, 'OTHER', %s, %s)""", (top, f"Kekurangan biaya aktual {row['number']}", amount))
        pg._event(conn, top, user, "SUBMIT", "DRAFT", "SUBMITTED", None, {"requested": amount, "settles": fund["number"]})
        details = {"fund": fund["number"], "topUp": number, "amount": float(amount)}
    elif what == "REFUND":
        conn.execute(f"UPDATE {P}.pengajuan SET refund_amount = %s, updated_at = now() WHERE id::text = %s", (amount, fund["id"]))
        pg._event(conn, fund["id"], user, "NOTE", None, None, f"Biaya aktual lebih kecil: dana kembali Rp {amount:,.0f}",
                  {"refund": amount})
        details = {"fund": fund["number"], "refund": float(amount)}
    else:
        details = {"fund": fund["number"], "even" if what == "EVEN" else "payActual": float(amount)}
        if what == "UNPAID_LESS":
            pg._event(conn, fund["id"], user, "NOTE", None, None,
                      f"Biaya aktual {row['number']} lebih kecil Rp {amount:,.0f} dari yang disetujui: bayar sesuai aktual", details)
    de_event(conn, row["id"], user, "FUND", None, what, None, details)
    return {"kind": what, "amount": float(amount)}


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
