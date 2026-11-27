"""Aggregator Insight stage 3 (migration 151, 2026-11-25): Reviews, Complaints, Void, Price & Menu and Reports.

Reviews    what BotFood keeps of the customers' reviews; the day is the order's, else the day BotFood got it. BotFood gives no
           merchant reply, so nothing is said about replied or not. The topic of a low review comes from its words (TOPICS).
Complaints tickets of the team: from a low review (one ticket per review) or entered by hand, with the first response, the
           resolution and the member handling the outlet that day (handle schedule).
Void       the void / cancelled bills of the delivery apps in ESB, with the reason the team gives each one, and the voids the
           team enters by hand (not in ESB).
Price&Menu per menu name and platform: the prices that differ between outlets, the menus most outlets have and one misses;
           price changes since 25 Nov 2026 (trigger); what sells better on a platform than in the others.
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mode
from typing import Any, Optional

from app import aggregator as agg
from app import database as db

P = agg.P
WIB = agg.WIB
LOW = 3  # a review of 3 stars or less
PLATFORMS = ("GoFood", "GrabFood", "ShopeeFood")
TOPICS: list[tuple[str, str]] = [
    ("Pesanan kurang / salah", r"kurang|salah|ga ?ada|gak ada|tidak ada|ketinggalan|beda|tertukar|ga ?dapet|ga ?dikasih|gadikasi|tidak di ?kasih|lupa"),
    ("Kemasan / tumpah", r"tumpah|bocor|kemasan|tutup|seal|rusak|penyok|plastik|sedotan"),
    ("Rasa / kualitas", r"rasa|hambar|manis|pahit|asam|basi|enak|encer|aneh|dingin|panas|gosong|keras|bau"),
    ("Es / suhu", r"\bes\b|mencair|cair|suhu|es batu"),
    ("Lama / pengantaran", r"lama|telat|lambat|nunggu|menunggu|driver|antar"),
    ("Pelayanan", r"kasir|pelayan|ramah|jutek|barista|judes|cancel|batal|respon"),
    ("Harga / porsi", r"mahal|harga|porsi|sedikit|dikit"),
]
COMPLAINT_STATUS = ("open", "progress", "done")
COMPLAINT_SOURCE = ("review", "chat", "app", "other")
# migration 167 (2026-11-27): the details of the handling and its evidence
ACTIONS = ("REFUND", "REPLACE", "VOUCHER", "APOLOGY", "REPLY", "OTHER")
CAUSES = ("OUTLET", "DRIVER", "PLATFORM", "CUSTOMER", "UNKNOWN")
CONTACTS = ("CHAT", "PHONE", "WHATSAPP", "REVIEW_REPLY")
BEARERS = ("OUTLET", "COMPANY", "PLATFORM")
EVIDENCE = ("CHAT", "REFUND", "VOUCHER", "REPLY", "PHOTO", "OTHER")
MAX_EVIDENCE = 10
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
VOID_REASONS = ("Stok habis", "Outlet tutup / belum buka", "Salah input", "Driver tidak datang", "Customer batal", "Sistem / aplikasi", "Lainnya")


class QualityError(ValueError):
    pass


def topic(comment: Optional[str]) -> str:
    """The topic of a review from its words (the first that matches). Pure (tested)."""
    c = (comment or "").lower()
    if not c.strip():
        return "Tanpa komentar"
    return next((name for name, rx in TOPICS if re.search(rx, c)), "Lainnya")


def _span(a: date, z: date) -> tuple[datetime, datetime]:
    return agg_at(a), agg_at(z + timedelta(days=1))


def agg_at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=WIB)


def _names() -> dict[int, tuple[Optional[str], str]]:
    """{BotFood outlet id: (our branch code, name)}."""
    return {r["bf_branch_id"]: (r["branch_code"], r["name"]) for r in db.fetch(
        f"""SELECT m.bf_branch_id, m.branch_code, coalesce(b.branch_name, m.bf_name) AS name FROM {P}.agg_branch_map m
             LEFT JOIN integration_esb.master_branches b ON b.branch_code = m.branch_code""")}


def _handlers(a: date, z: date) -> dict[tuple[date, str], str]:
    from app import aggregator_reports
    return aggregator_reports._handlers(a, z) if (z - a).days <= 92 else {}


# ---------------------------------------------------------------- reviews

def reviews(a: date, z: date, platform: Optional[str] = None, limit: int = 3000) -> dict:
    t0, t1 = _span(a, z)
    names = _names()
    rows = db.fetch(f"""SELECT id, bf_branch_id, outlet_name, channel, rating, comment, item_name, order_id, tags, customer_name, ordered_at IS NOT NULL AS by_order,
                               coalesce(ordered_at, created_at) AS at FROM {P}.agg_review
                         WHERE coalesce(ordered_at, created_at) >= %s AND coalesce(ordered_at, created_at) < %s
                           AND (%s::text IS NULL OR channel = %s)""", (t0, t1, platform, platform))
    rated = [r for r in rows if r["rating"] is not None]
    low = [r for r in rated if r["rating"] <= LOW]
    by: dict[tuple, list] = defaultdict(list)
    day: dict[str, list] = defaultdict(list)
    for r in rated:
        by[names.get(r["bf_branch_id"], (None, r["outlet_name"] or "—"))].append(r["rating"])
        day[r["at"].astimezone(WIB).date().isoformat()].append(r["rating"])
    who = _handlers(a, z)
    tickets = {str(r["review_id"]): r["id"] for r in db.fetch(f"SELECT id, review_id FROM {P}.agg_complaint WHERE review_id IS NOT NULL")}
    avg = lambda v: round(sum(v) / len(v), 2) if v else None  # noqa: E731
    shown = sorted(low + [r for r in rated if r["rating"] > LOW and (r["comment"] or "").strip()], key=lambda r: r["at"], reverse=True)[:limit]
    out = []
    for r in shown:
        code, name = names.get(r["bf_branch_id"], (None, r["outlet_name"] or "—"))
        d = r["at"].astimezone(WIB)
        out.append({"id": str(r["id"]), "at": d.isoformat(), "timeKind": "order" if r["by_order"] else "received", "outlet": name, "branchCode": code, "platform": r["channel"], "rating": r["rating"],
                    "comment": r["comment"], "item": r["item_name"], "orderId": r["order_id"], "tags": r["tags"], "topic": topic(r["comment"]) if r["rating"] <= LOW else None,
                    "member": who.get((d.date(), code)) if code else None, "complaintId": tickets.get(str(r["id"]))})
    return {"summary": {"n": len(rated), "avg": avg([float(r["rating"]) for r in rated]), "low": len(low), "withComment": sum(bool((r["comment"] or "").strip()) for r in rated),
                        "noTicket": sum(str(r["id"]) not in tickets for r in low), "limit": limit, "byOrderTime": sum(bool(r["by_order"]) for r in rated)},
            "byRating": [{"rating": k, "n": v} for k, v in sorted(Counter(int(r["rating"]) for r in rated).items(), reverse=True)],
            "byPlatform": [{"platform": p, "n": len(v), "avg": avg(v), "low": sum(x <= LOW for x in v)} for p in PLATFORMS
                           if (v := [float(r["rating"]) for r in rated if r["channel"] == p])],
            "byOutlet": sorted(({"outlet": k[1], "branchCode": k[0], "n": len(v), "avg": avg([float(x) for x in v]), "low": sum(x <= LOW for x in v)} for k, v in by.items()),
                               key=lambda x: (-x["low"], x["avg"] or 5)),
            "topics": [{"topic": k, "n": v} for k, v in Counter(topic(r["comment"]) for r in low).most_common()],
            "days": [{"day": k, "n": len(v), "avg": avg([float(x) for x in v])} for k, v in sorted(day.items())], "rows": out}


# ---------------------------------------------------------------- complaints

def _complaint(r: dict, names: dict[str, str], who: dict) -> dict:
    iso = lambda v: v.astimezone(WIB).isoformat() if v else None  # noqa: E731
    mins = lambda x: int((x - r["created_at"]).total_seconds() // 60) if x else None  # noqa: E731
    return {"id": r["id"], "reviewId": str(r["review_id"]) if r["review_id"] else None, "branchCode": r["branch_code"], "outlet": names.get(r["branch_code"], r["branch_code"] or "—"),
            "platform": r["channel"], "orderNo": r["order_no"], "day": r["happened_on"].isoformat(), "source": r["source"], "category": r["category"],
            "detail": r["detail"], "status": r["status"], "resolution": r["resolution"], "compensation": float(r["compensation"]) if r["compensation"] is not None else None,
            "createdAt": iso(r["created_at"]), "createdBy": r["created_by_name"], "firstResponseAt": iso(r["first_response_at"]), "resolvedAt": iso(r["resolved_at"]),
            "responseMin": mins(r["first_response_at"]), "resolveMin": mins(r["resolved_at"]), "handledBy": r["handled_by_name"],
            "member": who.get((r["happened_on"], r["branch_code"])) if r["branch_code"] else None,
            "action": r.get("action"), "rootCause": r.get("root_cause"), "contacted": r.get("contacted"), "contactVia": r.get("contact_via"),
            "replyText": r.get("reply_text"), "compBearer": r.get("comp_bearer"), "esbBill": r.get("esb_bill"),
            "evidence": int(r.get("evidence_n") or 0)}


def _branch_names() -> dict[str, str]:
    return {o["branch_code"]: o["branch_name"] for o in agg.outlets()}


def complaints(a: date, z: date, platform: Optional[str] = None) -> dict:
    rows = db.fetch(f"""SELECT c.*, (SELECT count(*) FROM {P}.agg_complaint_evidence e WHERE e.complaint_id = c.id) AS evidence_n
                          FROM {P}.agg_complaint c WHERE (happened_on BETWEEN %s AND %s OR status <> 'done') AND (%s::text IS NULL OR channel = %s)
                        ORDER BY (status = 'done'), happened_on DESC, id DESC LIMIT 2000""", (a, z, platform, platform))
    names, who = _branch_names(), _handlers(a, z)
    data = [_complaint(r, names, who) for r in rows]
    ok = [x for x in data if x["responseMin"] is not None]
    done = [x for x in data if x["resolveMin"] is not None]
    med = lambda v: sorted(v)[len(v) // 2] if v else None  # noqa: E731
    return {"summary": {"n": len(data), **{s: sum(x["status"] == s for x in data) for s in COMPLAINT_STATUS},
                        "responseMedian": med([x["responseMin"] for x in ok]), "resolveMedian": med([x["resolveMin"] for x in done]),
                        "compensation": sum(x["compensation"] or 0 for x in data)},
            "categories": [{"category": k, "n": v} for k, v in Counter(x["category"] for x in data).most_common()],
            "topics": [t for t, _ in TOPICS] + ["Lainnya"], "outlets": [{"code": k, "name": v} for k, v in names.items()], "data": data}


def _who(user: dict) -> str:
    return user.get("full_name") or user["username"]


def create_complaint(user: dict, body: dict) -> dict:
    rid = body.get("reviewId")
    if rid:
        r = db.fetchrow(f"SELECT *, coalesce(ordered_at, created_at) AS at FROM {P}.agg_review WHERE id::text = %s", (str(rid),))
        if r is None:
            raise QualityError("Review tidak ditemukan")
        have = db.fetchrow(f"SELECT id FROM {P}.agg_complaint WHERE review_id = %s", (r["id"],))
        if have:
            return {"id": have["id"], "existing": True}
        code, _ = _names().get(r["bf_branch_id"], (None, None))
        vals = (r["id"], code, r["channel"], r["order_id"] or None, r["at"].astimezone(WIB).date(), "review", topic(r["comment"]),
                (r["comment"] or f"Rating {r['rating']} tanpa komentar")[:2000])
    else:
        if body.get("source") not in COMPLAINT_SOURCE or body.get("platform") not in PLATFORMS:
            raise QualityError("Pilih sumber dan platform")
        if body.get("branchCode") not in _branch_names():
            raise QualityError("Pilih outlet")
        if not (body.get("detail") or "").strip():
            raise QualityError("Tulis isi komplain")
        day = date.fromisoformat(body["day"]) if body.get("day") else datetime.now(WIB).date()
        if day > datetime.now(WIB).date():
            raise QualityError("Tanggal tidak boleh di masa depan")
        vals = (None, body["branchCode"], body["platform"], (body.get("orderNo") or "").strip()[:60] or None, day, body["source"],
                (body.get("category") or topic(body["detail"]))[:60], body["detail"].strip()[:2000])
    with db.transaction() as conn:
        r = conn.execute(f"""INSERT INTO {P}.agg_complaint (review_id, branch_code, channel, order_no, happened_on, source, category, detail, created_by, created_by_name)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", (*vals, user["id"], _who(user))).fetchone()
        _ev(conn, r["id"], "CREATE", None, "open", "Dari ulasan" if rid else None, _who(user))
    return {"id": r["id"], "existing": False}


def update_complaint(user: dict, complaint_id: int, body: dict) -> dict:
    cur = db.fetchrow(f"SELECT * FROM {P}.agg_complaint WHERE id = %s", (complaint_id,))
    if cur is None:
        raise LookupError("Komplain tidak ditemukan")
    status = body.get("status", cur["status"])
    if status not in COMPLAINT_STATUS:
        raise QualityError("Status tidak dikenal")
    resolution = (body.get("resolution", cur["resolution"]) or "").strip()[:2000] or None
    details = handling(body, cur)
    if status == "done":
        missing = done_missing(resolution, details, evidence_count(complaint_id))
        if missing:
            raise QualityError("Belum bisa selesai: " + "; ".join(missing))
    comp = body.get("compensation", cur["compensation"])
    if comp is not None and float(comp) < 0:
        raise QualityError("Kompensasi tidak boleh minus")
    db.fetchrow(f"""UPDATE {P}.agg_complaint SET status = %s, category = %s, resolution = %s, compensation = %s, handled_by_name = %s, updated_at = now(),
                        first_response_at = CASE WHEN %s <> 'open' THEN coalesce(first_response_at, now()) ELSE first_response_at END,
                        resolved_at = CASE WHEN %s = 'done' THEN coalesce(resolved_at, now()) ELSE NULL END WHERE id = %s RETURNING id""",
                (status, (body.get("category") or cur["category"])[:60], resolution, comp, _who(user), status, status, complaint_id))
    db.fetchrow(f"""UPDATE {P}.agg_complaint SET action = %s, root_cause = %s, contacted = %s, contact_via = %s, reply_text = %s, comp_bearer = %s,
                           esb_bill = %s WHERE id = %s RETURNING id""",
                (details["action"], details["root_cause"], details["contacted"], details["contact_via"], details["reply_text"], details["comp_bearer"],
                 details["esb_bill"], complaint_id))
    with db.transaction() as conn:
        _ev(conn, complaint_id, "STATUS" if status != cur["status"] else "EDIT", cur["status"], status, resolution if status == "done" else None, _who(user))
    return complaint(complaint_id)


# ---------------------------------------------------------------- handling details & evidence (migration 167)

def _ev(conn, cid: int, kind: str, frm: Optional[str], to: Optional[str], note: Optional[str], by: str) -> None:
    conn.execute(f"INSERT INTO {P}.agg_complaint_event (complaint_id, kind, from_status, to_status, note, by_name) VALUES (%s, %s, %s, %s, %s, %s)",
                 (cid, kind, frm, to, (note or "")[:500] or None, by))


def handling(body: dict, cur: dict) -> dict:
    """The details of the handling from the body (a field not sent keeps its value); values checked."""
    def pick(key: str, col: str, allowed: tuple) -> Optional[str]:
        v = body.get(key, cur.get(col))
        if v in (None, ""):
            return None
        if v not in allowed:
            raise QualityError("Pilihan tidak dikenal")
        return v
    contacted = body.get("contacted", cur.get("contacted"))
    return {"action": pick("action", "action", ACTIONS), "root_cause": pick("rootCause", "root_cause", CAUSES),
            "contacted": None if contacted is None else bool(contacted), "contact_via": pick("contactVia", "contact_via", CONTACTS),
            "reply_text": ((body.get("replyText", cur.get("reply_text")) or "").strip()[:2000] or None),
            "comp_bearer": pick("compBearer", "comp_bearer", BEARERS), "esb_bill": ((body.get("esbBill", cur.get("esb_bill")) or "").strip()[:60] or None)}


def done_missing(resolution: Optional[str], d: dict, evidence: int) -> list[str]:
    """What a ticket still needs to be done (pure, tested): the resolution, the action, the root cause, whether the
    customer was contacted, and at least one evidence; a reply to the review when the action is a reply."""
    out = []
    if not resolution:
        out.append("tulis penyelesaian")
    if not d.get("action"):
        out.append("pilih tindakan")
    if not d.get("root_cause"):
        out.append("pilih penyebab")
    if d.get("contacted") is None:
        out.append("isi apakah pelanggan dihubungi")
    if d.get("action") == "REPLY" and not d.get("reply_text"):
        out.append("tulis balasan ulasan")
    if evidence < 1:
        out.append("lampirkan minimal 1 bukti penanganan")
    return out


def evidence_count(cid: int) -> int:
    return int(db.fetchrow(f"SELECT count(*) AS n FROM {P}.agg_complaint_evidence WHERE complaint_id = %s", (cid,))["n"])


def complaint(cid: int) -> dict:
    """One ticket with its review (when it came from one), its evidence and its history."""
    r = db.fetchrow(f"""SELECT c.*, (SELECT count(*) FROM {P}.agg_complaint_evidence e WHERE e.complaint_id = c.id) AS evidence_n
                          FROM {P}.agg_complaint c WHERE c.id = %s""", (cid,))
    if r is None:
        raise LookupError("Komplain tidak ditemukan")
    out = _complaint(r, _branch_names(), {})
    iso = lambda v: v.astimezone(WIB).isoformat() if v else None  # noqa: E731
    out["files"] = [{"id": str(e["id"]), "kind": e["kind"], "mime": e["mime"], "bytes": e["bytes"], "fileName": e["file_name"], "note": e["note"],
                     "by": e["created_by_name"], "at": iso(e["created_at"])}
                    for e in db.fetch(f"SELECT * FROM {P}.agg_complaint_evidence WHERE complaint_id = %s ORDER BY created_at", (cid,))]
    out["events"] = [{"kind": e["kind"], "from": e["from_status"], "to": e["to_status"], "note": e["note"], "by": e["by_name"], "at": iso(e["at"])}
                     for e in db.fetch(f"SELECT * FROM {P}.agg_complaint_event WHERE complaint_id = %s ORDER BY at, id", (cid,))]
    if r["review_id"]:
        rv = db.fetchrow(f"""SELECT rating, comment, coalesce(ordered_at, created_at) AS at, order_id, item_name, customer_name, merchant_reply,
                                    merchant_reply_at, tags FROM {P}.agg_review WHERE id = %s""", (r["review_id"],))
        if rv:  # the reply seen in the merchant app (BotFood) beside the one the team wrote: they should match
            out["review"] = {"rating": rv["rating"], "comment": rv["comment"], "at": iso(rv["at"]), "orderId": rv["order_id"], "item": rv["item_name"],
                             "customer": rv["customer_name"], "appReply": rv["merchant_reply"], "appReplyAt": iso(rv["merchant_reply_at"]), "tags": rv["tags"]}
    return out


def _evidence_type(data: bytes, mime: str) -> tuple[str, str]:
    sig = {b"\xff\xd8\xff": ("image/jpeg", "jpg"), b"\x89PNG": ("image/png", "png"), b"RIFF": ("image/webp", "webp"), b"%PDF": ("application/pdf", "pdf")}
    for head, out in sig.items():
        if data.startswith(head):
            return out
    raise QualityError("Bukti harus gambar (JPG, PNG, WebP) atau PDF")


def add_evidence(user: dict, cid: int, kind: str, data: bytes, file_name: Optional[str], note: Optional[str]) -> dict:
    import hashlib
    import uuid
    from app.damaged_equipment import media_root
    if kind not in EVIDENCE:
        raise QualityError("Jenis bukti tidak dikenal")
    if not data or len(data) > MAX_EVIDENCE_BYTES:
        raise QualityError("Ukuran bukti maksimal 10 MB")
    if db.fetchrow(f"SELECT 1 AS ok FROM {P}.agg_complaint WHERE id = %s", (cid,)) is None:
        raise LookupError("Komplain tidak ditemukan")
    if evidence_count(cid) >= MAX_EVIDENCE:
        raise QualityError(f"Maksimal {MAX_EVIDENCE} bukti per tiket")
    mime, ext = _evidence_type(data, "")
    if mime.startswith("image/"):
        from app.thumbs import fit
        small = fit(data)
        if small is not None and len(small) < len(data):
            data, mime, ext = small, "image/jpeg", "jpg"
    sha = hashlib.sha256(data).hexdigest()
    if db.fetchrow(f"SELECT 1 AS ok FROM {P}.agg_complaint_evidence WHERE complaint_id = %s AND sha256 = %s", (cid, sha)):
        raise QualityError("Bukti yang sama sudah dilampirkan")
    eid = str(uuid.uuid4())
    rel = f"aggregator/{datetime.now(WIB):%Y/%m}/{eid}.{ext}"
    path = media_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {P}.agg_complaint_evidence (id, complaint_id, kind, path, mime, bytes, sha256, file_name, note, created_by, created_by_name)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (eid, cid, kind, rel, mime, len(data), sha, (file_name or "")[:200] or None, (note or "").strip()[:500] or None, user["id"], _who(user)))
        conn.execute(f"UPDATE {P}.agg_complaint SET first_response_at = coalesce(first_response_at, now()), updated_at = now() WHERE id = %s", (cid,))
        _ev(conn, cid, "EVIDENCE", None, None, f"{kind}{': ' + note.strip()[:200] if note and note.strip() else ''}", _who(user))
    return complaint(cid)


def remove_evidence(user: dict, cid: int, eid: str) -> dict:
    from app.damaged_equipment import media_root
    with db.transaction() as conn:
        r = conn.execute(f"DELETE FROM {P}.agg_complaint_evidence WHERE id::text = %s AND complaint_id = %s RETURNING path, kind", (eid, cid)).fetchone()
        if r is None:
            raise LookupError("Bukti tidak ditemukan")
        st = conn.execute(f"SELECT status FROM {P}.agg_complaint WHERE id = %s", (cid,)).fetchone()
        if st and st["status"] == "done" and not conn.execute(f"SELECT 1 FROM {P}.agg_complaint_evidence WHERE complaint_id = %s", (cid,)).fetchone():
            raise QualityError("Tiket selesai harus punya minimal 1 bukti: tambah bukti lain dulu")
        _ev(conn, cid, "EVIDENCE_REMOVED", None, None, r["kind"], _who(user))
    (media_root() / r["path"]).unlink(missing_ok=True)
    return complaint(cid)


def evidence_file(eid: str) -> Optional[dict]:
    from app.damaged_equipment import media_root
    r = db.fetchrow(f"SELECT path, mime, file_name FROM {P}.agg_complaint_evidence WHERE id::text = %s", (eid,))
    return {"path": media_root() / r["path"], "mime": r["mime"], "name": r["file_name"]} if r else None


# ---------------------------------------------------------------- void

CHANNEL = "CASE lower(h.visit_purpose) WHEN 'gofood' THEN 'GoFood' WHEN 'grabfood' THEN 'GrabFood' ELSE 'ShopeeFood' END"


def voids(a: date, z: date, platform: Optional[str] = None) -> dict:
    from app.database import TABLE_TRANSACTIONS
    names, who = _branch_names(), _handlers(a, z)
    rows = db.fetch(
        f"""SELECT h.sales_num, h.bill_num, h.branch_code, {CHANNEL} AS channel, h.total_amount, h.sales_date::date AS day, h.sales_date_in, h.status,
                   h.raw_data->>'editedBy' AS edited, h.raw_data->>'additionalInfo' AS info, n.reason, n.note, n.by_name
              FROM {TABLE_TRANSACTIONS} h LEFT JOIN {P}.agg_void n ON n.sales_num = h.sales_num
             WHERE h.sales_date >= %s AND h.sales_date < %s::date + 1 AND h.status IN ('Void', 'Cancelled')
               AND lower(h.visit_purpose) IN ('gofood', 'grabfood', 'shopeefood') ORDER BY h.sales_date_in DESC""", (a, z))
    data = [{"key": r["sales_num"], "manual": False, "orderNo": r["info"] or r["bill_num"] or r["sales_num"], "bill": r["bill_num"], "branchCode": r["branch_code"],
             "outlet": names.get(r["branch_code"], r["branch_code"]), "platform": r["channel"], "day": r["day"].isoformat(),
             "time": r["sales_date_in"].strftime("%H:%M") if r["sales_date_in"] else None, "amount": float(r["total_amount"] or 0), "editedBy": r["edited"],
             "reason": r["reason"], "note": r["note"], "by": r["by_name"], "member": who.get((r["day"], r["branch_code"]))} for r in rows]
    for r in db.fetch(f"SELECT * FROM {P}.agg_void WHERE sales_num IS NULL AND happened_on BETWEEN %s AND %s ORDER BY happened_on DESC, id DESC", (a, z)):
        data.append({"key": f"m{r['id']}", "manual": True, "orderNo": r["order_no"], "bill": None, "branchCode": r["branch_code"], "outlet": names.get(r["branch_code"], r["branch_code"]),
                     "platform": r["channel"], "day": r["happened_on"].isoformat(), "time": None, "amount": float(r["amount"] or 0), "editedBy": None,
                     "reason": r["reason"], "note": r["note"], "by": r["by_name"], "member": who.get((r["happened_on"], r["branch_code"]))})
    if platform:
        data = [x for x in data if x["platform"] == platform]
    sales = db.fetchrow(
        f"""SELECT count(*) AS n FROM {TABLE_TRANSACTIONS} h WHERE h.sales_date >= %s AND h.sales_date < %s::date + 1 AND h.status = 'Finished'
             AND coalesce(h.bill_num, '') <> '' AND lower(h.visit_purpose) IN ('gofood', 'grabfood', 'shopeefood')
             AND (%s::text IS NULL OR lower(h.visit_purpose) = lower(%s))""", (a, z, platform, platform))["n"]
    by: dict[str, dict] = {}
    for x in data:
        o = by.setdefault(x["outlet"], {"outlet": x["outlet"], "branchCode": x["branchCode"], "n": 0, "amount": 0.0, "noReason": 0})
        o["n"] += 1
        o["amount"] += x["amount"]
        o["noReason"] += not x["reason"]
    n = len(data)
    return {"summary": {"n": n, "amount": sum(x["amount"] for x in data), "noReason": sum(not x["reason"] for x in data), "manual": sum(x["manual"] for x in data),
                        "bills": int(sales), "pct": round(n / (n + sales) * 100, 2) if n + sales else None},
            "reasons": [{"reason": k, "n": v} for k, v in Counter(x["reason"] or "Belum diisi" for x in data).most_common()],
            "byOutlet": sorted(by.values(), key=lambda o: -o["n"]), "choices": list(VOID_REASONS), "outlets": [{"code": k, "name": v} for k, v in names.items()],
            "data": data[:3000]}


def set_void_reason(user: dict, keys: list[str], reason: str, note: Optional[str]) -> int:
    if reason not in VOID_REASONS:
        raise QualityError("Alasan tidak dikenal")
    note = (note or "").strip()[:500] or None
    if reason == "Lainnya" and not note:
        raise QualityError("Tulis keterangan untuk alasan Lainnya")
    with db.transaction() as conn:
        for k in dict.fromkeys(keys):
            if re.fullmatch(r"m\d+", k):
                conn.execute(f"UPDATE {P}.agg_void SET reason = %s, note = %s, by_name = %s, updated_at = now() WHERE id = %s AND sales_num IS NULL", (reason, note, _who(user), int(k[1:])))
            else:
                conn.execute(f"""INSERT INTO {P}.agg_void (sales_num, reason, note, by_name, created_by) VALUES (%s, %s, %s, %s, %s)
                                 ON CONFLICT (sales_num) DO UPDATE SET reason = EXCLUDED.reason, note = EXCLUDED.note, by_name = EXCLUDED.by_name, updated_at = now()""",
                             (k, reason, note, _who(user), user["id"]))
    return len(keys)


def add_void(user: dict, body: dict) -> int:
    if body.get("platform") not in PLATFORMS or body.get("branchCode") not in _branch_names():
        raise QualityError("Pilih outlet dan platform")
    if body.get("reason") not in VOID_REASONS:
        raise QualityError("Pilih alasan")
    order = (body.get("orderNo") or "").strip()[:60]
    if not order:
        raise QualityError("Isi nomor order")
    day = date.fromisoformat(body["day"])
    if day > datetime.now(WIB).date():
        raise QualityError("Tanggal tidak boleh di masa depan")
    if float(body.get("amount") or 0) < 0:
        raise QualityError("Nominal tidak boleh minus")
    if db.fetchrow(f"SELECT 1 AS x FROM {P}.agg_void WHERE order_no = %s AND channel = %s AND sales_num IS NULL", (order, body["platform"])):
        raise QualityError("Order ini sudah dicatat")
    return db.fetchrow(f"""INSERT INTO {P}.agg_void (branch_code, channel, order_no, happened_on, amount, reason, note, by_name, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                       (body["branchCode"], body["platform"], order, day, body.get("amount") or 0, body["reason"], (body.get("note") or "").strip()[:500] or None,
                        _who(user), user["id"]))["id"]


# ---------------------------------------------------------------- price & menu

def menu_key(name: str) -> str:
    """The same menu written differently ("Es Kopi Calf  (Reguler)" = "es kopi calf reguler"). Pure (tested)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def price_spread(items: list[dict]) -> list[dict]:
    """The menus of one platform whose price differs between outlets: the usual price (most outlets) and who differs.
    items: {name, price, outlet, platform}. Pure (tested)."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for i in items:
        if i["price"]:
            by[(i["platform"], menu_key(i["name"]))].append(i)
    out = []
    for (p, _), v in by.items():
        prices = [float(x["price"]) for x in v]
        if len(v) < 3 or len(set(prices)) == 1:
            continue
        usual = mode(prices)
        diff = [{"outlet": x["outlet"], "price": float(x["price"])} for x in v if float(x["price"]) != usual]
        out.append({"platform": p, "menu": v[0]["name"], "outlets": len(v), "usual": usual, "min": min(prices), "max": max(prices),
                    "different": len(diff), "who": sorted(diff, key=lambda x: x["price"])})
    return sorted(out, key=lambda x: (-x["different"], x["menu"]))


def missing_menus(items: list[dict], share: float = 0.8) -> list[dict]:
    """The menus at least `share` of the outlets of a platform have and an outlet does not. Pure (tested)."""
    outlets: dict[str, set] = defaultdict(set)
    has: dict[tuple, set] = defaultdict(set)
    label: dict[tuple, str] = {}
    for i in items:
        outlets[i["platform"]].add(i["outlet"])
        k = (i["platform"], menu_key(i["name"]))
        has[k].add(i["outlet"])
        label.setdefault(k, i["name"])
    out = []
    for (p, k), have in has.items():
        if len(outlets[p]) >= 5 and len(have) >= share * len(outlets[p]):
            for o in sorted(outlets[p] - have):
                out.append({"platform": p, "outlet": o, "menu": label[(p, k)], "have": len(have), "of": len(outlets[p])})
    return sorted(out, key=lambda x: (x["outlet"], x["platform"], x["menu"]))


def menu(platform: Optional[str] = None) -> dict:
    names = _names()
    rows = db.fetch(f"""SELECT i.name, i.category, i.price, i.in_stock, c.channel, c.bf_branch_id, c.name AS cname FROM {P}.agg_item i
                          JOIN {P}.agg_channel c ON c.id = i.channel_id WHERE (%s::text IS NULL OR c.channel = %s)""", (platform, platform))
    packaging = lambda r: any(w in (r["category"] or "").upper() for w in ("PACKAG", "KEMASAN"))  # noqa: E731
    items = [{"name": r["name"], "price": float(r["price"]) if r["price"] is not None else None, "platform": r["channel"],
              "outlet": names.get(r["bf_branch_id"], (None, r["cname"]))[1], "code": names.get(r["bf_branch_id"], (None, None))[0]} for r in rows if not packaging(r)]
    code_of = {i["outlet"]: i["code"] for i in items}
    spread, missing = price_spread(items), [{**m, "branchCode": code_of.get(m["outlet"])} for m in missing_menus(items)]
    changes = [{"at": r["at"].astimezone(WIB).isoformat(), "menu": r["name"], "platform": r["channel"], "outlet": names.get(r["bf_branch_id"], (None, "—"))[1],
                "branchCode": names.get(r["bf_branch_id"], (None, "—"))[0], "old": float(r["old_price"]), "new": float(r["new_price"])} for r in db.fetch(
        f"""SELECT l.at, i.name, c.channel, c.bf_branch_id, l.old_price, l.new_price FROM {P}.agg_price_log l JOIN {P}.agg_item i ON i.id = l.item_id
             JOIN {P}.agg_channel c ON c.id = i.channel_id WHERE (%s::text IS NULL OR c.channel = %s) ORDER BY l.at DESC LIMIT 500""", (platform, platform))]
    return {"summary": {"items": len(items), "spread": len(spread), "differ": sum(x["different"] for x in spread), "missing": len(missing), "changes": len(changes)},
            "spread": spread[:800], "missing": missing[:3000], "changes": changes}


def fit(channel: Optional[str], branch: Optional[str], days: int = 30) -> dict:
    """What sells on a delivery app: the share of every menu on the platform against its share on the other channels
    (1.5 = half again as strong there). Menus of 20 portions or more."""
    z = datetime.now(WIB).date() - timedelta(days=1)
    a = z - timedelta(days=days - 1)
    rows = db.fetch(f"""SELECT menu_name, lower(channel) AS ch, sum(qty) AS qty, sum(subtotal) AS amount FROM {P}.agg_menu_daily
                         WHERE sales_date BETWEEN %s AND %s AND kind = 'menu' AND (%s::text IS NULL OR branch_code = %s) GROUP BY 1, 2""", (a, z, branch, branch))
    if not rows:
        rows = db.fetch(f"""SELECT menu_name, lower(channel) AS ch, sum(qty) AS qty, sum(subtotal) AS amount FROM {P}.agg_menu_daily
                             WHERE sales_date BETWEEN %s AND %s AND (%s::text IS NULL OR branch_code = %s) GROUP BY 1, 2""", (a, z, branch, branch))
    on = {channel.lower()} if channel else {p.lower() for p in PLATFORMS}
    here: Counter = Counter()
    rest: Counter = Counter()
    money: Counter = Counter()
    for r in rows:
        if r["ch"] in on:
            here[r["menu_name"]] += float(r["qty"] or 0)
            money[r["menu_name"]] += float(r["amount"] or 0)
        else:
            rest[r["menu_name"]] += float(r["qty"] or 0)
    th, tr = sum(here.values()) or 1, sum(rest.values()) or 1
    out = []
    for m in set(here) | set(rest):
        if here[m] + rest[m] < 20:
            continue
        sh, sr = here[m] / th, rest[m] / tr
        out.append({"menu": m, "qty": here[m], "amount": money[m], "share": round(sh * 100, 2), "shareElsewhere": round(sr * 100, 2),
                    "lift": round(sh / sr, 2) if sr else None})
    out.sort(key=lambda x: -x["qty"])
    return {"from": a.isoformat(), "to": z.isoformat(), "data": out[:300]}


# ---------------------------------------------------------------- hours per outlet (those BotFood has none for)

def hours() -> dict[str, dict]:
    """The hours entered per outlet before the merchant app became the reference: used only where nothing else is known."""
    try:
        return {r["branch_code"]: r for r in db.fetch(f"SELECT branch_code, open_time, close_time, by_name FROM {P}.agg_hours")}
    except Exception:  # noqa: BLE001 - before migration 151
        return {}


def hours_checks() -> dict[tuple[str, str], list[dict]]:
    """(branch, platform) to the checks of the team against the merchant app, newest first."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    try:
        for r in db.fetch(f"SELECT * FROM {P}.agg_hours_check ORDER BY valid_from DESC"):
            out[(r["branch_code"], r["channel"])].append(r)
    except Exception:  # noqa: BLE001 - before migration 154
        return {}
    return out


def hours_list() -> list[dict]:
    """Per outlet and platform: the hours BotFood has, where they come from, and the last check of the team. `state`:
    unchecked / ok / diff / recheck (the usual hours are not those of the check any more)."""
    from app import aggregator_reports as rep
    z = datetime.now(WIB).date()
    known, checks, names = rep.app_hours(z), hours_checks(), _branch_names()
    f = lambda t: t.strftime("%H:%M") if t else None  # noqa: E731
    out = []
    for c in db.fetch(f"""SELECT c.id, c.channel, m.branch_code FROM {P}.agg_channel c JOIN {P}.agg_branch_map m ON m.bf_branch_id = c.bf_branch_id
                          WHERE m.branch_code IS NOT NULL"""):
        if c["branch_code"] not in names:
            continue
        a, k = known.get(c["id"]), rep.checked(checks.get((c["branch_code"], c["channel"]), []), z)
        usual = a["usual"] if a else None
        state = "unchecked" if not k else "recheck" if k["app_hours"] != usual else k["status"]
        out.append({"code": c["branch_code"], "name": names[c["branch_code"]], "platform": c["channel"], "app": usual, "kinds": a["kinds"] if a else 0,
                    "from": ("botfood" if a["setting"] else "app") if a else None, "state": state, "status": k["status"] if k else None,
                    "open": f(k["open_time"]) if k else None, "close": f(k["close_time"]) if k else None, "by": k["by_name"] if k else None,
                    "at": k["checked_at"].isoformat() if k else None, "validFrom": k["valid_from"].isoformat() if k else None})
    return sorted(out, key=lambda r: (r["name"], r["platform"]))


def check_hours(user: dict, rows: list[dict]) -> None:
    """The team looked in the merchant app: the same as BotFood (ok) or the hours it really shows (diff), from a date on."""
    have = {(r["code"], r["platform"]): r for r in hours_list()}
    now = datetime.now(WIB).date()
    with db.transaction() as conn:
        for r in rows:
            x = have.get((r["code"], r["platform"]))
            if not x:
                raise QualityError("Outlet atau platform tidak dikenal")
            if r["status"] == "ok" and not x["app"]:
                raise QualityError("Jam di BotFood belum ada: isi jam yang tampil di aplikasi merchant")
            if r["status"] == "diff" and not (r.get("open") and r.get("close") and r["open"] < r["close"]):
                raise QualityError("Isi jam buka dan jam tutup; jam tutup harus setelah jam buka")
            diff = r["status"] == "diff"
            day = r.get("validFrom") or now
            if day > now:
                raise QualityError("Tanggal berlaku tidak boleh setelah hari ini")
            conn.execute(f"""INSERT INTO {P}.agg_hours_check (branch_code, channel, valid_from, status, open_time, close_time, app_hours, by_name)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (branch_code, channel, valid_from) DO UPDATE SET status = EXCLUDED.status, open_time = EXCLUDED.open_time,
                               close_time = EXCLUDED.close_time, app_hours = EXCLUDED.app_hours, by_name = EXCLUDED.by_name, checked_at = now()""",
                         (r["code"], r["platform"], day, r["status"], r["open"] if diff else None, r["close"] if diff else None, x["app"], _who(user)))


# ---------------------------------------------------------------- overview & reports

def brief(a: date, z: date) -> dict:
    """The figures of the Overview page."""
    rv, cp, vd = reviews(a, z, limit=0), complaints(a, z), voids(a, z)
    m = menu()["summary"]
    return {"from": a.isoformat(), "to": z.isoformat(), "reviews": rv["summary"], "topics": rv["topics"][:5], "complaints": cp["summary"], "voids": vd["summary"],
            "worst": rv["byOutlet"][:5], "menu": m}


def report(a: date, z: date, platform: Optional[str] = None) -> dict[str, Any]:
    """Everything of a period per outlet: the sheets of the Reports page (Summary first)."""
    from app import aggregator_reports
    op = aggregator_reports.opening(a, z, platform)
    oos = aggregator_reports.oos(a, z, platform, False, 14)
    rv, cp, vd = reviews(a, z, platform), complaints(a, z, platform), voids(a, z, platform)
    per: dict[str, dict] = defaultdict(lambda: {"days": 0, "on": 0, "late": 0, "closed": 0, "oos": 0, "reviews": 0, "avg": None, "low": 0, "complaints": 0, "voids": 0, "void": 0.0, "members": set()})
    for r in op["rows"]:
        if r["status"] in ("on_time", "late", "not_opened"):
            p = per[r["outlet"]]
            p["days"] += 1
            p["on"] += r["status"] == "on_time"
            p["late"] += r["status"] == "late"
            p["closed"] += r["status"] == "not_opened"
            if r["member"]:
                p["members"].add(r["member"])
    for x in oos["items"]:
        per[x["outlet"]]["oos"] += x["times"]
    for x in rv["byOutlet"]:
        per[x["outlet"]].update(reviews=x["n"], avg=x["avg"], low=x["low"])
    for x in cp["data"]:
        per[x["outlet"]]["complaints"] += 1
    for x in vd["byOutlet"]:
        per[x["outlet"]].update(voids=x["n"], void=x["amount"])
    s = op["summary"]
    summary = [["Hari-outlet dihitung", s["counted"]], ["Tepat waktu (minimal)", s["onTime"]],
               ["Tepat waktu % (minimal)", round(s["onTime"] / s["counted"] * 100, 1) if s["counted"] else None], ["Terlambat (maksimal)", s["late"]],
               ["Tidak buka", s["notOpened"]], ["Item kosong sekarang", oos["summary"]["outNow"]], ["Kali kosong dalam periode", oos["summary"]["timesInPeriod"]],
               ["Review", rv["summary"]["n"]], ["Rating rata-rata", rv["summary"]["avg"]], ["Review rendah (≤ 3)", rv["summary"]["low"]],
               ["Komplain", cp["summary"]["n"]], ["Komplain selesai", cp["summary"]["done"]], ["Void", vd["summary"]["n"]], ["Nilai void (Rp)", round(vd["summary"]["amount"])],
               ["Void % dari order", vd["summary"]["pct"]], ["Void tanpa alasan", vd["summary"]["noReason"]]]
    return {"from": a.isoformat(), "to": z.isoformat(), "summary": summary, "sheets": [
        {"sheet": "Summary", "title": f"Aggregator {a:%d/%m/%Y} – {z:%d/%m/%Y}" + (f" · {platform}" if platform else ""), "header": ["Ukuran", "Nilai"], "rows": summary},
        {"sheet": "Per outlet", "header": ["Outlet", "Hari-outlet", "Tepat waktu (min)", "Terlambat (maks)", "Tidak buka", "Kali item kosong", "Review", "Rating", "Review rendah",
                                           "Komplain", "Void", "Nilai void", "Ditangani"],
         "rows": [[k, p["days"], p["on"], p["late"], p["closed"], p["oos"], p["reviews"], p["avg"], p["low"], p["complaints"], p["voids"], round(p["void"]), ", ".join(sorted(p["members"]))]
                  for k, p in sorted(per.items())]},
        {"sheet": "Topik review rendah", "header": ["Topik", "Jumlah"], "rows": [[x["topic"], x["n"]] for x in rv["topics"]]},
        {"sheet": "Review rendah", "header": ["Waktu", "Outlet", "Platform", "Rating", "Topik", "Komentar", "Item", "Order", "Ditangani"],
         "rows": [[x["at"][:16].replace("T", " "), x["outlet"], x["platform"], x["rating"], x["topic"], x["comment"], x["item"], x["orderId"], x["member"]] for x in rv["rows"] if x["rating"] <= LOW]},
        {"sheet": "Komplain", "header": ["Tanggal", "Outlet", "Platform", "Order", "Sumber", "Kategori", "Isi", "Status", "Penyelesaian", "Kompensasi", "Respons (menit)", "Selesai (menit)", "Ditangani"],
         "rows": [[x["day"], x["outlet"], x["platform"], x["orderNo"], x["source"], x["category"], x["detail"], x["status"], x["resolution"], x["compensation"], x["responseMin"], x["resolveMin"], x["handledBy"]] for x in cp["data"]]},
        {"sheet": "Void", "header": ["Tanggal", "Jam", "Outlet", "Platform", "Order / bill", "Nominal", "Alasan", "Keterangan", "Sumber", "Diisi oleh"],
         "rows": [[x["day"], x["time"], x["outlet"], x["platform"], x["orderNo"], x["amount"], x["reason"], x["note"], "Manual" if x["manual"] else "ESB", x["by"]] for x in vd["data"]]},
        {"sheet": "Alasan void", "header": ["Alasan", "Jumlah"], "rows": [[x["reason"], x["n"]] for x in vd["reasons"]]},
    ]}
