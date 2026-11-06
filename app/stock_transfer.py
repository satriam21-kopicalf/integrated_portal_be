"""Monitoring Stock: the Stock List of every branch and the Transfer Stock between outlets (TITO, migration 075).

Stock List   the stock of a branch from its latest stock opname in the portal (kopicalf core, the current lines: base
             unit + WIP), with the transfers received since that count (in / out) and the transfers still on their way
             out (requested, accepted, approved): available = SO + in − out − pending out. Valued with the Pricelist
             (else the ESB HPP).
Transfer     the requester (outlet A, a PIC of A or the outlet account) asks outlet B for materials: the exact
             quantities (no partial transfer), each one at most what B has available; a material B has none of (≤ 0)
             cannot be asked. A photo and a signature come with the request.
               submitted  → B accepts or declines with a note (the stock does not move)
               accepted   → Cost Control (the approvers set in Monitoring Stock → Settings) approves or rejects; when
                            approving it may adjust the quantities to the need of the outlets (migration 076, a note
                            required, the requested quantity kept), within what the source has
               approved   → A receives the goods with a clear photo of them: only then the stock moves (B out, A in),
                            valued with the Pricelist of that day (app/stock_moves.py transferIn / transferOut)
             A request is cancelled by A while B has not answered. Recorded in the portal only: Cost Control labels
             each transfer whether it is to be entered in ESB (esb_label esb | portal).
"""
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db
from app.pic_checkin import _names, branch_options, parse_photo

P = "integration_portal"
T, T_LINE, T_APPROVER = f"{P}.stock_transfer", f"{P}.stock_transfer_line", f"{P}.stock_transfer_approver"
PENDING = ("submitted", "accepted", "approved")  # on their way out of the source: not available any more
LATEST_SO_DAYS = 60
MAX_LINES = 60


class TransferError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def _exec(query: str, params: Any) -> int:
    with db.transaction() as conn:
        return conn.execute(query, params).rowcount


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _who(user: dict) -> str:
    return user.get("full_name") or user["username"]


# ---------------------------------------------------------------- stock

def items_info(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    return {r["code"]: r for r in db.fetch(
        """SELECT i.code, i.name, upper(coalesce(i.base_uom, '')) AS unit, coalesce(c.name, '') AS category
             FROM kc_master.item i LEFT JOIN kc_master.item_category c ON c.id = i.category_id WHERE i.code = ANY(%s)""",
        (codes,))}


def prices(codes: list[str], day: Optional[date] = None) -> dict[str, float]:
    """The Pricelist of the day, else the ESB HPP (as the Sales Calculator and the waste value)."""
    from app import menu_costing as mc
    day = day or date.today()
    out = mc.prices_on(day, codes or None)
    missing = [c for c in codes if c not in out]
    if missing:
        hpp = mc.esb_hpp(day)
        out.update({c: hpp[c][0] for c in missing if c in hpp})
    return out


def stock(branches: Optional[list[str]], exclude: Optional[str] = None) -> dict[str, dict]:
    """{branch: {so: {date, refs}, items: {code: {so, in, out, pending, available, unit}}}} from the latest portal
    stock opname of each branch (the last LATEST_SO_DAYS days). `exclude`: a transfer left out of the pending (its own)."""
    from app import menu_costing as mc
    counts = mc.portal_counts(date.today(), branches, back=LATEST_SO_DAYS, ahead=0)
    out: dict[str, dict] = {}
    for b, c in counts.items():
        out[b] = {"so": {"date": c["date"], "refs": c["refs"]},
                  "items": {code: {"so": q, "in": 0.0, "out": 0.0, "pending": 0.0, "unit": u} for code, (q, u) in c["items"].items()}}
    if not out:
        return out
    codes = list(out)
    rows = db.fetch(
        f"""SELECT t.id::text, t.status, t.to_branch, t.from_branch, t.received_at, l.item_code, l.unit, l.qty
              FROM {T} t JOIN {T_LINE} l ON l.transfer_id = t.id
             WHERE (t.to_branch = ANY(%(b)s) OR t.from_branch = ANY(%(b)s))
               AND (t.status = ANY(%(pending)s) OR (t.status = 'received' AND t.received_at > now() - make_interval(days => %(days)s)))""",
        {"b": codes, "pending": list(PENDING), "days": LATEST_SO_DAYS + 1})
    for r in rows:
        if r["id"] == exclude:
            continue
        qty = _f(r["qty"])
        if r["status"] == "received":
            day = r["received_at"].date().isoformat()
            for side, key in ((r["to_branch"], "in"), (r["from_branch"], "out")):
                s = out.get(side)
                if s and day >= s["so"]["date"]:  # received after the count (a count of that day already holds it)
                    it = s["items"].setdefault(r["item_code"], {"so": 0.0, "in": 0.0, "out": 0.0, "pending": 0.0, "unit": r["unit"]})
                    it[key] += qty
        elif r["from_branch"] in out:
            it = out[r["from_branch"]]["items"].setdefault(r["item_code"], {"so": 0.0, "in": 0.0, "out": 0.0, "pending": 0.0, "unit": r["unit"]})
            it["pending"] += qty
    for s in out.values():
        for it in s["items"].values():
            it["available"] = round(it["so"] + it["in"] - it["out"] - it["pending"], 6)
    return out


TRANSFER_KINDS = ("transferIn", "transferOut")


def stock_list(branches: Optional[list[str]]) -> dict:
    """Monitoring Stock → Stock List: one row per branch × item with its value, and per branch its latest count; with the
    running stock (app/running_stock.py: the estimate now, from the SO and every move, waste and usage since)."""
    from app import running_stock
    s = stock(branches)
    try:
        run = running_stock.running(branches)
    except Exception:  # noqa: BLE001 - the estimate is extra: the Stock List of the counts still shows
        run = {}
    codes = sorted({c for v in s.values() for c in v["items"]} | {c for v in run.values() for c in v["items"]})
    info, price = items_info(codes), prices(codes)
    names = _names(list(s))
    rows, heads = [], []
    for b, v in sorted(s.items(), key=lambda kv: names.get(kv[0], kv[0])):
        value = est_value = 0.0
        r = run.get(b, {"items": {}, "so": {}})
        for code in sorted(set(v["items"]) | set(r["items"])):
            it = v["items"].get(code) or {"so": 0.0, "in": 0.0, "out": 0.0, "pending": 0.0, "available": 0.0, "unit": r["items"][code]["unit"]}
            e = r["items"].get(code)
            p = price.get(code)
            now = it["so"] + it["in"] - it["out"]
            val = round(now * p, 2) if p is not None else None
            value += val or 0.0
            est = e["estimate"] if e else now
            ev = round(est * p, 2) if p is not None else None
            est_value += ev or 0.0
            mv = (e or {}).get("moves", {})
            rows.append({"branch": b, "branchName": names.get(b, b), "code": code, "name": (info.get(code) or {}).get("name") or code,
                         "category": (info.get(code) or {}).get("category") or "", "unit": it["unit"], "so": round(it["so"], 3),
                         "in": round(it["in"], 3), "out": round(it["out"], 3), "pending": round(it["pending"], 3),
                         "stock": round(now, 3), "available": round(it["available"], 3), "price": p, "value": val,
                         # the running stock since the count: goods received and the other moves, waste approved, usage by the cards
                         "received": round(mv.get("received", 0.0), 3),
                         "otherMoves": round(sum(q for k, q in mv.items() if k not in TRANSFER_KINDS and k != "received"), 3),
                         "waste": round((e or {}).get("waste", 0.0), 3), "used": round((e or {}).get("used", 0.0), 3),
                         "estimate": round(est, 3), "estimateValue": ev})
        heads.append({"branch": b, "branchName": names.get(b, b), "soDate": v["so"]["date"], "refs": v["so"]["refs"],
                      "final": r["so"].get("final"), "items": len(v["items"]), "value": round(value, 2), "estimateValue": round(est_value, 2)})
    return {"branches": heads, "rows": rows, "estimatedAt": date.today().isoformat()}


# ---------------------------------------------------------------- transfers

def _number(conn) -> str:
    prefix = f"TS-{date.today():%y%m}-"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('stock_transfer_number'))")  # one number per request
    n = conn.execute(f"SELECT count(*) AS n FROM {T} WHERE number LIKE %s", (prefix + "%",)).fetchone()["n"]
    return f"{prefix}{n + 1:04d}"


def _mine(user: dict) -> set[str]:
    return {b["code"] for b in branch_options(user)}


def is_approver(user: dict) -> bool:
    if user.get("role") == "superadmin":
        return True
    return db.fetchrow(f"SELECT 1 FROM {T_APPROVER} WHERE user_id::text = %s", (str(user["id"]),)) is not None


def create(user: dict, body: dict) -> dict:
    to_b, from_b = str(body.get("toBranch") or ""), str(body.get("fromBranch") or "")
    mine = _mine(user)
    if to_b not in mine:
        raise TransferError("Outlet peminta bukan outlet akun Anda")
    if from_b == to_b:
        raise TransferError("Pilih outlet tujuan selain outlet Anda sendiri")
    every = {b["code"]: b["name"] for b in _sources_for(user)}
    if from_b not in every:
        raise TransferError("Outlet tujuan tidak ditemukan")
    lines = [ln for ln in body.get("lines") or [] if ln.get("code")]
    if not lines:
        raise TransferError("Pilih minimal satu material")
    if len(lines) > MAX_LINES:
        raise TransferError(f"Maksimal {MAX_LINES} material per pengajuan")
    if len({ln["code"] for ln in lines}) != len(lines):
        raise TransferError("Material yang sama dipilih dua kali")
    if not body.get("photo") or not body.get("signature"):
        raise TransferError("Lampirkan foto evidence dan tanda tangan sebelum submit")
    src = stock([from_b]).get(from_b)
    if not src:
        raise TransferError(f"{every[from_b]} belum punya Stock Opname di portal dalam {LATEST_SO_DAYS} hari terakhir")
    info = items_info([ln["code"] for ln in lines])
    clean = []
    for ln in lines:
        code, qty = str(ln["code"]), _f(ln.get("qty"))
        it = src["items"].get(code)
        name = (info.get(code) or {}).get("name") or code
        if qty <= 0:
            raise TransferError(f"Qty {name} harus lebih dari 0")
        if not it or it["available"] <= 0:
            raise TransferError(f"Stok {name} di {every[from_b]} kosong atau minus: tidak bisa diambil")
        if qty > it["available"] + 1e-9:
            raise TransferError(f"Qty {name} melebihi stok {every[from_b]} ({it['available']:g} {it['unit']})")
        clean.append((code, name, it["unit"], qty, it["available"]))
    pt, photo = parse_photo(body["photo"])
    st, sign = parse_photo(body["signature"])
    with db.transaction() as conn:
        row = conn.execute(
            f"""INSERT INTO {T} (number, to_branch, to_name, from_branch, from_name, note, requested_by, requested_name,
                    photo, photo_type, signature, signature_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text, number""",
            (_number(conn), to_b, every.get(to_b) or to_b, from_b, every[from_b], (body.get("note") or "").strip()[:500] or None,
             user["id"], _who(user), photo, pt, sign, st)).fetchone()
        for i, (code, name, unit, qty, avail) in enumerate(clean, 1):
            conn.execute(f"""INSERT INTO {T_LINE} (transfer_id, line_no, item_code, item_name, unit, qty, requested_qty, available)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", (row["id"], i, code, name, unit, qty, qty, avail))
    return detail(row["id"])


def _load(transfer_id: str) -> dict:
    row = db.fetchrow(f"SELECT id::text, number, status, to_branch, from_branch, requested_by::text FROM {T} WHERE id::text = %s", (transfer_id,))
    if not row:
        raise TransferError("Transfer tidak ditemukan")
    return row


def answer(user: dict, transfer_id: str, accept: bool, note: Optional[str]) -> dict:
    """The source outlet checks the request: accept (the stock does not move yet) or decline, with a note."""
    t = _load(transfer_id)
    if t["from_branch"] not in _mine(user):
        raise TransferError("Hanya outlet tujuan yang bisa memvalidasi pengajuan ini")
    if t["status"] != "submitted":
        raise TransferError("Pengajuan ini sudah divalidasi")
    note = (note or "").strip()[:500] or None
    if not note:
        raise TransferError("Tulis alasan / catatan validasi")
    if accept:  # still in stock (another request may have taken it meanwhile)
        _check_available(transfer_id, t["from_branch"])
    _exec(f"""UPDATE {T} SET status = %s, source_note = %s, source_by = %s, source_at = now() WHERE id::text = %s AND status = 'submitted'""",
               ("accepted" if accept else "declined", note, _who(user), transfer_id))
    return detail(transfer_id)


def _check_available(transfer_id: str, from_b: str) -> None:
    src = (stock([from_b], exclude=transfer_id).get(from_b) or {"items": {}})["items"]
    for ln in db.fetch(f"SELECT item_code, item_name, unit, qty FROM {T_LINE} WHERE transfer_id::text = %s", (transfer_id,)):
        avail = (src.get(ln["item_code"]) or {}).get("available", 0.0)
        if _f(ln["qty"]) > avail + 1e-9:
            raise TransferError(f"Stok {ln['item_name']} tidak cukup lagi ({avail:g} {ln['unit']} tersedia)")


def decide(user: dict, transfer_id: str, approve: bool, note: Optional[str], qty: Optional[dict[str, float]] = None) -> dict:
    """Cost Control approves or rejects a transfer the source accepted; `qty` {item code: quantity}: the quantities
    adjusted when approving (each > 0 and at most what the source has; a note required when one changes)."""
    if not is_approver(user):
        raise TransferError("Akun Anda bukan approver Transfer Stock (Monitoring Stock → Pengaturan)")
    t = _load(transfer_id)
    if t["status"] != "accepted":
        raise TransferError("Hanya transfer yang sudah disetujui outlet tujuan yang bisa di-approve")
    note = (note or "").strip()[:500] or None
    if not approve and not note:
        raise TransferError("Tulis alasan penolakan")
    changes: list[tuple[float, str]] = []
    if approve and qty:
        lines = {r["item_code"]: r for r in db.fetch(f"SELECT item_code, item_name, qty FROM {T_LINE} WHERE transfer_id::text = %s", (transfer_id,))}
        for code, q in qty.items():
            if code not in lines:
                raise TransferError("Material tidak ada di transfer ini")
            q = _f(q)
            if q <= 0:
                raise TransferError(f"Qty {lines[code]['item_name']} harus lebih dari 0")
            if abs(q - _f(lines[code]["qty"])) > 1e-9:
                changes.append((q, code))
        if changes and not note:
            raise TransferError("Tulis alasan penyesuaian qty")
    if approve:
        with db.transaction() as conn:  # the adjusted quantities first, then the stock check counts them
            for q, code in changes:
                conn.execute(f"UPDATE {T_LINE} SET qty = %s WHERE transfer_id::text = %s AND item_code = %s", (q, transfer_id, code))
        try:
            _check_available(transfer_id, t["from_branch"])
        except TransferError:
            if changes:  # back to what it was: nothing approved
                with db.transaction() as conn:
                    for _, code in changes:
                        conn.execute(f"UPDATE {T_LINE} SET qty = requested_qty WHERE transfer_id::text = %s AND item_code = %s", (transfer_id, code))
            raise
    _exec(f"""UPDATE {T} SET status = %s, cc_note = %s, cc_by = %s, cc_at = now() WHERE id::text = %s AND status = 'accepted'""",
               ("approved" if approve else "rejected", note, _who(user), transfer_id))
    return detail(transfer_id)


def receive(user: dict, transfer_id: str, photo: Optional[str], note: Optional[str]) -> dict:
    """The requester receives the goods: a clear photo first; the stock of both outlets moves now, valued today."""
    t = _load(transfer_id)
    if t["to_branch"] not in _mine(user):
        raise TransferError("Hanya outlet peminta yang bisa menerima barang ini")
    if t["status"] != "approved":
        raise TransferError("Barang bisa diterima setelah transfer di-approve Cost Control")
    if not photo:
        raise TransferError("Lampirkan foto barang yang diterima terlebih dahulu")
    pt, data = parse_photo(photo)
    lines = db.fetch(f"SELECT line_no, item_code, qty FROM {T_LINE} WHERE transfer_id::text = %s", (transfer_id,))
    price = prices([ln["item_code"] for ln in lines])
    total = 0.0
    with db.transaction() as conn:
        for ln in lines:
            p = price.get(ln["item_code"])
            v = round(_f(ln["qty"]) * p, 2) if p is not None else None
            total += v or 0.0
            conn.execute(f"UPDATE {T_LINE} SET price = %s, value = %s WHERE transfer_id::text = %s AND line_no = %s",
                         (p, v, transfer_id, ln["line_no"]))
        conn.execute(f"""UPDATE {T} SET status = 'received', receive_photo = %s, receive_photo_type = %s, receive_note = %s,
                             received_by = %s, received_at = now(), value = %s WHERE id::text = %s AND status = 'approved'""",
                     (data, pt, (note or "").strip()[:500] or None, _who(user), round(total, 2), transfer_id))
    from app import running_stock
    running_stock.clear()
    return detail(transfer_id)


def cancel(user: dict, transfer_id: str) -> dict:
    t = _load(transfer_id)
    if t["to_branch"] not in _mine(user):
        raise TransferError("Hanya outlet peminta yang bisa membatalkan")
    if t["status"] != "submitted":
        raise TransferError("Pengajuan yang sudah divalidasi outlet tujuan tidak bisa dibatalkan")
    _exec(f"UPDATE {T} SET status = 'cancelled' WHERE id::text = %s AND status = 'submitted'", (transfer_id,))
    return detail(transfer_id)


def label(user: dict, transfer_id: str, value: Optional[str], note: Optional[str]) -> dict:
    """Cost Control: whether the transfer is to be entered in ESB (esb), stays in the portal (portal) or not decided."""
    if value not in ("esb", "portal", None):
        raise TransferError("Label: esb atau portal")
    _load(transfer_id)
    _exec(f"""UPDATE {T} SET esb_label = %s, esb_label_note = %s, esb_label_by = %s, esb_label_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END
                   WHERE id::text = %s""", (value, (note or "").strip()[:300] or None, _who(user) if value else None, value, transfer_id))
    return detail(transfer_id)


COLS = """t.id::text, t.number, t.to_branch, t.to_name, t.from_branch, t.from_name, t.status, t.note, t.requested_by::text,
    t.requested_name, t.requested_at, t.source_note, t.source_by, t.source_at, t.cc_note, t.cc_by, t.cc_at, t.receive_note,
    t.received_by, t.received_at, t.value, t.esb_label, t.esb_label_note, t.esb_label_by, t.esb_label_at,
    t.receive_photo IS NOT NULL AS received_photo,
    (SELECT count(*) FROM {line} l WHERE l.transfer_id = t.id)::int AS lines,
    (SELECT sum(l.qty) FROM {line} l WHERE l.transfer_id = t.id) AS qty"""


def _iso(v):
    return v.isoformat() if v else None


def _view(r: dict) -> dict:
    return {"id": r["id"], "number": r["number"], "status": r["status"], "toBranch": r["to_branch"], "toName": r["to_name"],
            "fromBranch": r["from_branch"], "fromName": r["from_name"], "note": r["note"], "lines": r["lines"],
            "requested": {"by": r["requested_name"], "at": _iso(r["requested_at"])},
            "source": {"note": r["source_note"], "by": r["source_by"], "at": _iso(r["source_at"])} if r["source_at"] else None,
            "costControl": {"note": r["cc_note"], "by": r["cc_by"], "at": _iso(r["cc_at"])} if r["cc_at"] else None,
            "received": {"note": r["receive_note"], "by": r["received_by"], "at": _iso(r["received_at"])} if r["received_at"] else None,
            "value": _f(r["value"]) if r["value"] is not None else None,
            "esbLabel": {"value": r["esb_label"], "note": r["esb_label_note"], "by": r["esb_label_by"], "at": _iso(r["esb_label_at"])} if r["esb_label"] else None}


def detail(transfer_id: str) -> dict:
    r = db.fetchrow(f"SELECT {COLS.format(line=T_LINE)} FROM {T} t WHERE t.id::text = %s", (transfer_id,))
    if not r:
        raise TransferError("Transfer tidak ditemukan")
    v = _view(r)
    lines = db.fetch(f"SELECT * FROM {T_LINE} WHERE transfer_id::text = %s ORDER BY line_no", (transfer_id,))
    est = prices([ln["item_code"] for ln in lines]) if r["status"] != "received" else {}
    v["items"] = [{"code": ln["item_code"], "name": ln["item_name"], "unit": ln["unit"], "qty": _f(ln["qty"]),
                   "requested": _f(ln["requested_qty"]) if ln.get("requested_qty") is not None else _f(ln["qty"]),
                   "available": _f(ln["available"]) if ln["available"] is not None else None,
                   "price": _f(ln["price"]) if ln["price"] is not None else est.get(ln["item_code"]),
                   "value": _f(ln["value"]) if ln["value"] is not None else (round(_f(ln["qty"]) * est[ln["item_code"]], 2) if ln["item_code"] in est else None)}
                  for ln in lines]
    if v["value"] is None:
        v["estimate"] = round(sum(i["value"] or 0 for i in v["items"]), 2)
    return v


def listing(date_from: date, date_to: date, branches: Optional[list[str]] = None, status: str = "", side: str = "",
            limit: int = 1000) -> dict:
    """The transfers requested in the period; branches: where either side is one of them (side to / from: that side only)."""
    where = ["(t.requested_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %(a)s AND %(b)s"]
    params: dict[str, Any] = {"a": date_from, "b": date_to, "br": branches or [], "st": status, "lim": limit}
    if branches is not None:
        where.append({"to": "t.to_branch = ANY(%(br)s)", "from": "t.from_branch = ANY(%(br)s)"}.get(
            side, "(t.to_branch = ANY(%(br)s) OR t.from_branch = ANY(%(br)s))"))
    if status:
        where.append("t.status = %(st)s")
    rows = db.fetch(f"SELECT {COLS.format(line=T_LINE)} FROM {T} t WHERE {' AND '.join(where)} ORDER BY t.requested_at DESC LIMIT %(lim)s", params)
    data = [_view(r) for r in rows]
    by = lambda s: sum(1 for d in data if d["status"] == s)  # noqa: E731
    return {"data": data, "summary": {"total": len(data), **{s: by(s) for s in ("submitted", "accepted", "declined", "approved", "rejected", "received", "cancelled")},
                                      "value": round(sum(d["value"] or 0 for d in data), 2),
                                      "unlabelled": sum(1 for d in data if d["status"] == "received" and not d["esbLabel"])},
            "truncated": len(rows) >= limit}


def lines_of(ids: list[str]) -> list[dict]:
    """Every line of the transfers (the Excel per material)."""
    if not ids:
        return []
    return [{"id": r["id"], "code": r["item_code"], "name": r["item_name"], "unit": r["unit"], "qty": _f(r["qty"]),
             "requested": _f(r["requested_qty"]) if r.get("requested_qty") is not None else _f(r["qty"]),
             "price": _f(r["price"]) if r["price"] is not None else None, "value": _f(r["value"]) if r["value"] is not None else None}
            for r in db.fetch(f"SELECT transfer_id::text AS id, * FROM {T_LINE} WHERE transfer_id::text = ANY(%s) ORDER BY 1, line_no", (ids,))]


def _sources_for(user: dict) -> list[dict]:
    """The outlets a user may ask: the real outlets, or for a test branch the other test branches only (2026-11-07)."""
    from app import test_branches
    mine = [b["code"] for b in branch_options(user)]
    if any(test_branches.is_test(c) for c in mine):
        names = _names(list(test_branches.sources()))
        return [{"code": c, "name": names.get(c, c)} for c in sorted(test_branches.sources())]
    return [{"code": b["code"], "name": b["name"]} for b in branch_options({}, every=True)]


def form(user: dict) -> dict:
    mine = branch_options(user)
    codes = [b["code"] for b in mine]
    every = _sources_for(user)
    d_to = date.today()
    d_from = d_to - timedelta(days=60)
    outgoing = listing(d_from, d_to, codes, side="to", limit=100)["data"]
    incoming = listing(d_from, d_to, codes, side="from", limit=100)["data"]
    return {"branches": [{"code": b["code"], "name": b["name"]} for b in mine], "sources": every,
            "requests": outgoing, "incoming": incoming,
            "todo": {"answer": sum(1 for t in incoming if t["status"] == "submitted"),
                     "receive": sum(1 for t in outgoing if t["status"] == "approved")}}


def available_of(branch: str) -> dict:
    """The materials a source outlet has available (> 0), for the request form."""
    s = stock([branch]).get(branch)
    if not s:
        return {"so": None, "items": []}
    codes = sorted(s["items"])
    info = items_info(codes)
    items = [{"code": c, "name": (info.get(c) or {}).get("name") or c, "category": (info.get(c) or {}).get("category") or "",
              "unit": it["unit"], "available": round(it["available"], 3)} for c, it in s["items"].items() if it["available"] > 0]
    items.sort(key=lambda x: (x["category"], x["name"]))
    return {"so": s["so"], "items": items}


def file_of(transfer_id: str, kind: str) -> Optional[dict]:
    col = {"photo": ("photo", "photo_type"), "signature": ("signature", "signature_type"), "receive": ("receive_photo", "receive_photo_type")}[kind]
    return db.fetchrow(f"SELECT to_branch, from_branch, {col[0]} AS data, {col[1]} AS type FROM {T} WHERE id::text = %s", (transfer_id,))


def approvers() -> list[dict]:
    return [{"id": r["id"], "name": r["full_name"] or r["username"], "username": r["username"], "role": r["role"], "addedBy": r["added_by"]}
            for r in db.fetch(f"""SELECT u.id::text, u.username, u.full_name, u.role, a.added_by FROM {T_APPROVER} a
                                  JOIN {P}.user_account u ON u.id = a.user_id ORDER BY 3, 2""")]


def candidates() -> list[dict]:
    """The accounts that may be made approvers: Cost Control (and superadmins)."""
    return [{"id": r["id"], "name": r["full_name"] or r["username"], "username": r["username"], "role": r["role"]}
            for r in db.fetch(f"""SELECT id::text, username, full_name, role FROM {P}.user_account
                                  WHERE role IN ('cost_control', 'superadmin') AND coalesce(is_active, true) ORDER BY 3, 2""")]


def set_approvers(user: dict, ids: list[str]) -> list[dict]:
    allowed = {c["id"] for c in candidates()}
    bad = [i for i in ids if i not in allowed]
    if bad:
        raise TransferError("Approver hanya akun Cost Control")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {T_APPROVER}")
        for i in sorted(set(ids)):
            conn.execute(f"INSERT INTO {T_APPROVER} (user_id, added_by) VALUES (%s, %s)", (i, _who(user)))
    return approvers()


def moves(d_from: date, d_to: date, branches: list[str]) -> list[dict]:
    """The transfers received in the period (app/stock_moves.py): per branch, item and unit, in (+) or out (−)."""
    return db.fetch(
        f"""SELECT x.branch, x.kind, l.item_code, l.unit, sum(x.sign * l.qty) AS qty
              FROM {T} t JOIN {T_LINE} l ON l.transfer_id = t.id
              CROSS JOIN LATERAL (VALUES (t.to_branch, 'transferIn', 1), (t.from_branch, 'transferOut', -1)) x(branch, kind, sign)
             WHERE t.status = 'received' AND (t.received_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s AND x.branch = ANY(%s)
             GROUP BY 1, 2, 3, 4""", (d_from, d_to, branches))

