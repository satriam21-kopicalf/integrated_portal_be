"""Building & Renovation projects (2026-11-27, Building team; migration 164, page mon.building).

A project is a new outlet, a renovation or a major maintenance: where (an existing outlet or the planned one: name,
address, city, area), who (PIC, contractor), when (plan and actual dates) and the budget. Its RAB is made of sections
(Persiapan, Sipil, MEP …) and lines: a material of the catalog (description, unit and price taken from it, the price
date kept) or a typed line (labour, other), qty × unit price, the store, and the actual quantity and amount once bought.
The RAB is sent (SUBMITTED) and approved (page mon.building-approve or a superadmin): every approval keeps the RAB as a
version; changing an approved RAB makes it a draft again, to be approved as the next version.

The work is followed in stages (weight in %, plan start / end): a progress report sets a stage's progress with a note
and photos. The project's progress is the weighted progress of its stages; the plan (S-curve) is the weighted share of
every stage's plan days passed at a date.
"""
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db
from app.building import BuildingError, _text, price_of

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
KINDS = {"NEW_OUTLET": "Pembangunan outlet baru", "RENOVATION": "Renovasi outlet", "MAINTENANCE": "Maintenance besar"}
STATUS = {"PLANNING": "Perencanaan", "IN_PROGRESS": "Berjalan", "HANDOVER": "Serah terima", "DONE": "Selesai", "ON_HOLD": "Ditunda",
          "CANCELLED": "Dibatalkan"}
NEXT = {"PLANNING": ("IN_PROGRESS", "ON_HOLD", "CANCELLED"), "IN_PROGRESS": ("HANDOVER", "ON_HOLD", "CANCELLED"),
        "HANDOVER": ("DONE", "IN_PROGRESS"), "ON_HOLD": ("PLANNING", "IN_PROGRESS", "CANCELLED"), "DONE": (), "CANCELLED": ()}
RAB_STATUS = {"DRAFT": "Draf", "SUBMITTED": "Menunggu approval", "APPROVED": "Disetujui"}
LINE_KINDS = {"MATERIAL": "Material", "LABOUR": "Upah", "OTHER": "Lain-lain"}
SECTIONS = ("Persiapan & Perizinan", "Pekerjaan Sipil", "MEP (Listrik, Air, AC)", "CCTV, Alarm & Internet", "Furniture", "Lighting",
            "Upah Tenaga Kerja", "Lain-lain")
STAGES = {  # (name, weight %) per kind
    "NEW_OUTLET": (("Persiapan & perizinan", 5), ("Pekerjaan sipil & struktur", 30), ("MEP (listrik, air, AC)", 20),
                   ("Finishing (cat, lantai, plafon)", 20), ("Furniture & lighting", 15), ("CCTV & internet", 5), ("Serah terima", 5)),
    "RENOVATION": (("Persiapan & bongkar", 10), ("Pekerjaan sipil", 25), ("MEP (listrik, air, AC)", 20), ("Finishing", 25),
                   ("Furniture & lighting", 15), ("Serah terima", 5)),
    "MAINTENANCE": (("Persiapan", 10), ("Pekerjaan", 80), ("Serah terima", 10)),
}
MAX_PHOTOS = 6


def _today() -> date:
    return datetime.now(WIB).date()


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


def _date(v: Any) -> Optional[date]:
    return date.fromisoformat(str(v)[:10]) if v else None


def _by(user: dict) -> str:
    return user.get("full_name") or user.get("username") or "?"


# ---------------------------------------------------------------- pure figures (tested)

def rab_totals(sections: list[dict], lines: list[dict]) -> dict:
    """{total, actual, sections: {id: {total, actual}}, kinds: {MATERIAL: …}}: qty × unit price per line; actual = the
    amounts bought (lines without an actual are not counted in it)."""
    out = {"total": 0.0, "actual": 0.0, "sections": {s["id"]: {"total": 0.0, "actual": 0.0, "lines": 0} for s in sections},
           "kinds": {k: 0.0 for k in LINE_KINDS}, "bought": 0, "lines": len(lines)}
    for ln in lines:
        amount = round(float(ln["qty"] or 0) * float(ln["unit_price"] or 0), 2)
        out["total"] += amount
        out["kinds"][ln["kind"]] = out["kinds"].get(ln["kind"], 0.0) + amount
        sec = out["sections"].setdefault(ln["section_id"], {"total": 0.0, "actual": 0.0, "lines": 0})
        sec["total"] += amount
        sec["lines"] += 1
        if ln.get("actual_amount") is not None:
            out["actual"] += float(ln["actual_amount"])
            sec["actual"] += float(ln["actual_amount"])
            out["bought"] += 1
    out["total"], out["actual"] = round(out["total"], 2), round(out["actual"], 2)
    return out


def overall(stages: list[dict]) -> float:
    """The project's progress: Σ weight × progress / Σ weight (0 without weight)."""
    w = sum(float(s["weight"] or 0) for s in stages)
    return round(sum(float(s["weight"] or 0) * float(s["progress"] or 0) for s in stages) / w, 1) if w else 0.0


def planned_at(stages: list[dict], day: date) -> float:
    """The planned progress at `day`: every stage counts its weight × the share of its plan days passed."""
    w = sum(float(s["weight"] or 0) for s in stages)
    if not w:
        return 0.0
    done = 0.0
    for s in stages:
        a, b = s.get("plan_start"), s.get("plan_end")
        if not a or not b:
            continue
        if day >= b:
            share = 1.0
        elif day < a:
            share = 0.0
        else:
            share = ((day - a).days + 1) / ((b - a).days + 1)
        done += float(s["weight"] or 0) * share
    return round(done / w * 100, 1)


def curve(stages: list[dict], reports: list[dict], start: Optional[date], end: Optional[date], today: date) -> list[dict]:
    """The S-curve, one point per week from the plan start: {day, plan, actual} (actual up to today: the progress of every
    stage as last reported on or before the day)."""
    starts = [s["plan_start"] for s in stages if s.get("plan_start")]
    ends = [s["plan_end"] for s in stages if s.get("plan_end")]
    a = start or (min(starts) if starts else None)
    b = end or (max(ends) if ends else None)
    if not a or not b or b < a:
        return []
    weight = {s["id"]: float(s["weight"] or 0) for s in stages}
    total = sum(weight.values()) or 1.0
    reps = sorted(reports, key=lambda r: (r["reported_on"], r["id"]))
    points, day = [], a
    while True:
        point = {"day": day.isoformat(), "plan": planned_at(stages, day), "actual": None}
        if day <= today:
            last: dict[int, float] = {}
            for r in reps:
                if r["reported_on"] <= day and r.get("stage_id") in weight and r.get("progress") is not None:
                    last[r["stage_id"]] = float(r["progress"])
            point["actual"] = round(sum(weight[k] * v for k, v in last.items()) / total, 1)
        points.append(point)
        if day >= b:
            break
        day = min(day + timedelta(days=7), b)
    return points


# ---------------------------------------------------------------- projects

def _next_number(conn) -> str:
    month = _today().strftime("%y%m")
    n = conn.execute(f"""INSERT INTO {P}.building_counter (month, last) VALUES (%s, 1)
                         ON CONFLICT (month) DO UPDATE SET last = {P}.building_counter.last + 1 RETURNING last""", (month,)).fetchone()["last"]
    return f"BP-{month}-{n:04d}"


FIELDS = {"name": 200, "branch_code": 40, "location_name": 200, "address": 500, "city": 100, "business_unit": 60, "pic_name": 120,
          "contractor": 200, "note": 2000}
CAMEL = {"branchCode": "branch_code", "locationName": "location_name", "businessUnit": "business_unit", "picName": "pic_name",
         "areaM2": "area_m2", "planStart": "plan_start", "planEnd": "plan_end", "actualStart": "actual_start", "actualEnd": "actual_end"}


def _clean(body: dict) -> dict:
    body = {CAMEL.get(k, k): v for k, v in body.items()}
    kind = str(body.get("kind") or "").upper()
    if kind not in KINDS:
        raise BuildingError("Pilih jenis proyek")
    vals = {f: _text(body.get(f), n) for f, n in FIELDS.items()}
    if not vals["name"]:
        raise BuildingError("Nama proyek wajib diisi")
    if not vals["branch_code"] and not vals["location_name"]:
        raise BuildingError("Pilih outlet, atau isi nama lokasi untuk outlet baru")
    dates = {f: _date(body.get(f)) for f in ("plan_start", "plan_end", "actual_start", "actual_end")}
    if dates["plan_start"] and dates["plan_end"] and dates["plan_end"] < dates["plan_start"]:
        raise BuildingError("Target selesai harus setelah tanggal mulai")
    area = price_of(body.get("area_m2"))
    return {"kind": kind, **vals, **dates, "area_m2": area, "budget": price_of(body.get("budget"))}


def create(user: dict, body: dict) -> dict:
    v = _clean(body)
    with db.transaction() as conn:
        number = _next_number(conn)
        cols = list(v)
        pid = conn.execute(f"""INSERT INTO {P}.building_project (number, {', '.join(cols)}, created_by, updated_by)
                               VALUES (%s, {', '.join(['%s'] * len(cols))}, %s, %s) RETURNING id::text""",
                           (number, *v.values(), _by(user), _by(user))).fetchone()["id"]
        for i, name in enumerate(SECTIONS):
            conn.execute(f"INSERT INTO {P}.building_rab_section (project_id, sort, name) VALUES (%s, %s, %s)", (pid, i, name))
        _default_stages(conn, pid, v["kind"], v["plan_start"], v["plan_end"])
    return full(pid)


def _default_stages(conn, pid: str, kind: str, start: Optional[date], end: Optional[date]) -> None:
    """The stages of the kind, their plan dates spread over the project's plan by weight (one after the other)."""
    stages = STAGES[kind]
    days = (end - start).days + 1 if start and end else None
    at = start
    for i, (name, w) in enumerate(stages):
        a = b = None
        if days and at:
            span = max(1, round(days * w / 100))
            a, b = at, min(end, at + timedelta(days=span - 1)) if i < len(stages) - 1 else end
            at = min(end, b + timedelta(days=1))
        conn.execute(f"INSERT INTO {P}.building_stage (project_id, sort, name, weight, plan_start, plan_end) VALUES (%s, %s, %s, %s, %s, %s)",
                     (pid, i, name, w, a, b))


def update(user: dict, pid: str, body: dict) -> dict:
    v = _clean(body)
    with db.transaction() as conn:
        n = conn.execute(f"""UPDATE {P}.building_project SET {', '.join(f'{c} = %s' for c in v)}, updated_at = now(), updated_by = %s
                             WHERE id::text = %s""", (*v.values(), _by(user), pid)).rowcount
        if not n:
            raise LookupError("Proyek tidak ditemukan")
    return full(pid)


def set_status(user: dict, pid: str, to: str, note: Optional[str]) -> dict:
    row = _row(pid)
    if to not in NEXT.get(row["status"], ()):
        raise BuildingError(f"Proyek {STATUS[row['status']].lower()} tidak dapat dipindah ke {STATUS.get(to, to).lower()}")
    if to == "IN_PROGRESS" and row["rab_status"] != "APPROVED" and row["rab_version"] == 0:
        raise BuildingError("RAB harus disetujui sebelum proyek berjalan")
    if to in ("ON_HOLD", "CANCELLED") and not (note or "").strip():
        raise BuildingError("Isi alasan")
    sets = {"IN_PROGRESS": "actual_start = coalesce(actual_start, %(d)s)", "DONE": "actual_end = coalesce(actual_end, %(d)s)"}.get(to)
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.building_project SET status = %(to)s, {sets + ', ' if sets else ''}
                                note = CASE WHEN %(note)s::text IS NULL THEN note ELSE concat_ws(E'\\n', note, %(note)s::text) END,
                                updated_at = now(), updated_by = %(by)s WHERE id::text = %(id)s""",
                     {"to": to, "d": _today(), "note": f"[{STATUS[to]} {_today():%d/%m/%Y}] {note.strip()}" if (note or "").strip() else None,
                      "by": _by(user), "id": pid})
    return full(pid)


def _row(pid: str) -> dict:
    row = db.fetchrow(f"SELECT * FROM {P}.building_project WHERE id::text = %s", (pid,))
    if row is None:
        raise LookupError("Proyek tidak ditemukan")
    return row


def _view(r: dict, totals: Optional[dict] = None, progress: Optional[float] = None) -> dict:
    return {"id": str(r["id"]), "number": r["number"], "kind": r["kind"], "kindLabel": KINDS[r["kind"]], "name": r["name"],
            "branchCode": r["branch_code"], "branchName": r.get("branch_name"), "locationName": r["location_name"], "address": r["address"],
            "city": r["city"], "areaM2": _f(r["area_m2"]), "businessUnit": r["business_unit"], "picName": r["pic_name"],
            "contractor": r["contractor"], "planStart": _iso(r["plan_start"]), "planEnd": _iso(r["plan_end"]),
            "actualStart": _iso(r["actual_start"]), "actualEnd": _iso(r["actual_end"]), "budget": _f(r["budget"]),
            "status": r["status"], "statusLabel": STATUS[r["status"]], "next": list(NEXT[r["status"]]),
            "rabStatus": r["rab_status"], "rabStatusLabel": RAB_STATUS[r["rab_status"]], "rabVersion": r["rab_version"], "note": r["note"],
            "createdBy": r["created_by"], "createdAt": _iso(r["created_at"]), "updatedAt": _iso(r["updated_at"]), "updatedBy": r["updated_by"],
            "rabTotal": totals["total"] if totals else _f(r.get("rab_total")), "actualTotal": totals["actual"] if totals else _f(r.get("actual_total")),
            "progress": progress if progress is not None else _f(r.get("progress")),
            "late": bool(r["plan_end"] and r["status"] not in ("DONE", "CANCELLED") and r["plan_end"] < _today())}


BRANCH = f"""LEFT JOIN LATERAL (SELECT b.branch_name FROM integration_esb.master_branches b WHERE b.branch_code = p.branch_code LIMIT 1) br ON true"""


def projects() -> list[dict]:
    rows = db.fetch(f"""SELECT p.*, br.branch_name,
                               (SELECT sum(l.qty * l.unit_price) FROM {P}.building_rab_line l WHERE l.project_id = p.id) AS rab_total,
                               (SELECT sum(l.actual_amount) FROM {P}.building_rab_line l WHERE l.project_id = p.id) AS actual_total,
                               (SELECT CASE WHEN sum(s.weight) > 0 THEN round(sum(s.weight * s.progress) / sum(s.weight), 1) ELSE 0 END
                                  FROM {P}.building_stage s WHERE s.project_id = p.id) AS progress
                          FROM {P}.building_project p {BRANCH}
                         ORDER BY (p.status IN ('DONE', 'CANCELLED')), p.created_at DESC""")
    return [_view(r) for r in rows]


def full(pid: str) -> dict:
    r = db.fetchrow(f"SELECT p.*, br.branch_name FROM {P}.building_project p {BRANCH} WHERE p.id::text = %s", (pid,))
    if r is None:
        raise LookupError("Proyek tidak ditemukan")
    sections = db.fetch(f"SELECT * FROM {P}.building_rab_section WHERE project_id::text = %s ORDER BY sort, id", (pid,))
    lines = db.fetch(f"""SELECT l.*, v.name AS vendor_name, m.price AS catalog_price, m.price_on AS catalog_on
                           FROM {P}.building_rab_line l
                           LEFT JOIN {P}.building_vendor v ON v.id = l.vendor_id
                           LEFT JOIN {P}.building_material m ON m.id = l.material_id
                          WHERE l.project_id::text = %s ORDER BY l.section_id, l.sort, l.id""", (pid,))
    stages = db.fetch(f"SELECT * FROM {P}.building_stage WHERE project_id::text = %s ORDER BY sort, id", (pid,))
    reports = db.fetch(f"""SELECT g.*, s.name AS stage_name,
                                  coalesce((SELECT json_agg(f.id::text ORDER BY f.created_at) FROM {P}.building_photo f WHERE f.progress_id = g.id), '[]') AS photos
                             FROM {P}.building_progress g LEFT JOIN {P}.building_stage s ON s.id = g.stage_id
                            WHERE g.project_id::text = %s ORDER BY g.reported_on DESC, g.id DESC""", (pid,))
    versions = db.fetch(f"""SELECT version, total, approved_by, approved_at, note FROM {P}.building_rab_version
                             WHERE project_id::text = %s ORDER BY version DESC""", (pid,))
    totals = rab_totals(sections, lines)
    progress = overall(stages)
    today = _today()
    v = _view(r, totals, progress)
    v.update({
        "sections": [{"id": s["id"], "name": s["name"], "sort": s["sort"], **totals["sections"].get(s["id"], {"total": 0, "actual": 0, "lines": 0})} for s in sections],
        "lines": [{"id": ln["id"], "sectionId": ln["section_id"], "sort": ln["sort"], "kind": ln["kind"], "kindLabel": LINE_KINDS[ln["kind"]],
                   "materialId": ln["material_id"], "description": ln["description"], "spec": ln["spec"], "unit": ln["unit"], "qty": _f(ln["qty"]),
                   "unitPrice": _f(ln["unit_price"]), "amount": round(float(ln["qty"]) * float(ln["unit_price"]), 2), "priceOn": _iso(ln["price_on"]),
                   "catalogPrice": _f(ln["catalog_price"]), "catalogOn": _iso(ln["catalog_on"]),
                   "vendorId": ln["vendor_id"], "vendor": ln["vendor_name"] or ln["vendor_text"], "note": ln["note"],
                   "actualQty": _f(ln["actual_qty"]), "actualAmount": _f(ln["actual_amount"]), "updatedBy": ln["updated_by"]} for ln in lines],
        "kinds": totals["kinds"], "bought": totals["bought"],
        "stages": [{"id": s["id"], "name": s["name"], "sort": s["sort"], "weight": _f(s["weight"]), "planStart": _iso(s["plan_start"]),
                    "planEnd": _iso(s["plan_end"]), "progress": _f(s["progress"]), "doneOn": _iso(s["done_on"]),
                    "late": bool(s["plan_end"] and float(s["progress"]) < 100 and s["plan_end"] < today)} for s in stages],
        "reports": [{"id": g["id"], "stageId": g["stage_id"], "stage": g["stage_name"], "progress": _f(g["progress"]), "note": g["note"],
                     "reportedOn": _iso(g["reported_on"]), "by": g["created_by"], "at": _iso(g["created_at"]), "photos": g["photos"]} for g in reports],
        "versions": [{"version": x["version"], "total": _f(x["total"]), "by": x["approved_by"], "at": _iso(x["approved_at"]), "note": x["note"]} for x in versions],
        "plannedToday": planned_at(stages, today),
        "curve": curve(stages, reports, r["plan_start"], r["plan_end"], today),
    })
    return v


# ---------------------------------------------------------------- RAB

def _rab_open(conn, pid: str, user: dict) -> dict:
    """The RAB may be changed while the project is not done; an approved RAB changed becomes a draft (next version)."""
    row = conn.execute(f"SELECT status, rab_status FROM {P}.building_project WHERE id::text = %s FOR UPDATE", (pid,)).fetchone()
    if row is None:
        raise LookupError("Proyek tidak ditemukan")
    if row["status"] in ("DONE", "CANCELLED"):
        raise BuildingError("RAB proyek yang sudah selesai / dibatalkan tidak dapat diubah")
    if row["rab_status"] != "DRAFT":
        conn.execute(f"UPDATE {P}.building_project SET rab_status = 'DRAFT', updated_at = now(), updated_by = %s WHERE id::text = %s", (_by(user), pid))
    return row


def save_section(user: dict, pid: str, section_id: Optional[int], name: str) -> dict:
    name = _text(name, 120)
    if not name:
        raise BuildingError("Nama bagian wajib diisi")
    with db.transaction() as conn:
        _rab_open(conn, pid, user)
        if section_id is None:
            conn.execute(f"""INSERT INTO {P}.building_rab_section (project_id, sort, name)
                             VALUES (%s, (SELECT coalesce(max(sort), 0) + 1 FROM {P}.building_rab_section WHERE project_id::text = %s), %s)""", (pid, pid, name))
        elif not conn.execute(f"UPDATE {P}.building_rab_section SET name = %s WHERE id = %s AND project_id::text = %s", (name, section_id, pid)).rowcount:
            raise LookupError("Bagian tidak ditemukan")
    return full(pid)


def delete_section(user: dict, pid: str, section_id: int) -> dict:
    with db.transaction() as conn:
        _rab_open(conn, pid, user)
        if conn.execute(f"SELECT 1 FROM {P}.building_rab_line WHERE section_id = %s LIMIT 1", (section_id,)).fetchone():
            raise BuildingError("Kosongkan baris bagian ini dulu")
        conn.execute(f"DELETE FROM {P}.building_rab_section WHERE id = %s AND project_id::text = %s", (section_id, pid))
    return full(pid)


def save_line(user: dict, pid: str, line_id: Optional[int], body: dict) -> dict:
    kind = str(body.get("kind") or "MATERIAL").upper()
    if kind not in LINE_KINDS:
        raise BuildingError("Jenis baris tidak dikenal")
    material = int(body["materialId"]) if body.get("materialId") else None
    mat = db.fetchrow(f"SELECT * FROM {P}.building_material WHERE id = %s", (material,)) if material else None
    if material and mat is None:
        raise BuildingError("Material katalog tidak ditemukan")
    desc = _text(body.get("description"), 300) or (" ".join(x for x in (mat["category"], mat["spec"], mat["detail"]) if x) if mat else None)
    if not desc:
        raise BuildingError("Uraian wajib diisi")
    qty = float(body.get("qty") or 0)
    if qty <= 0 or qty > 1e7:
        raise BuildingError("Volume tidak valid")
    price = price_of(body.get("unitPrice"))
    if price is None:
        price = mat["price"] if mat and mat["price"] is not None else Decimal(0)
    price_on = _date(body.get("priceOn")) or (mat["price_on"] if mat and body.get("unitPrice") in (None, "") else _today())
    vendor_id = int(body["vendorId"]) if body.get("vendorId") else (mat["vendor_id"] if mat else None)
    actual = price_of(body.get("actualAmount"))
    aq = float(body["actualQty"]) if body.get("actualQty") not in (None, "") else None
    section = int(body.get("sectionId") or 0)
    vals = (section, kind, material, desc, _text(body.get("spec"), 200), _text(body.get("unit"), 30) or (mat["unit"] if mat else None), qty, price,
            price_on, vendor_id, None if vendor_id else _text(body.get("vendor"), 200), _text(body.get("note"), 500), aq, actual, _by(user))
    with db.transaction() as conn:
        _rab_open(conn, pid, user)
        if not conn.execute(f"SELECT 1 FROM {P}.building_rab_section WHERE id = %s AND project_id::text = %s", (section, pid)).fetchone():
            raise BuildingError("Pilih bagian RAB")
        if line_id is None:
            conn.execute(f"""INSERT INTO {P}.building_rab_line (section_id, kind, material_id, description, spec, unit, qty, unit_price, price_on,
                                     vendor_id, vendor_text, note, actual_qty, actual_amount, updated_by, project_id, sort)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                     (SELECT coalesce(max(sort), 0) + 1 FROM {P}.building_rab_line WHERE section_id = %s))""", (*vals, pid, section))
        elif not conn.execute(f"""UPDATE {P}.building_rab_line SET section_id = %s, kind = %s, material_id = %s, description = %s, spec = %s, unit = %s,
                                         qty = %s, unit_price = %s, price_on = %s, vendor_id = %s, vendor_text = %s, note = %s, actual_qty = %s,
                                         actual_amount = %s, updated_by = %s, updated_at = now()
                                   WHERE id = %s AND project_id::text = %s""", (*vals, line_id, pid)).rowcount:
            raise LookupError("Baris RAB tidak ditemukan")
    return full(pid)


def save_actual(user: dict, pid: str, line_id: int, actual_qty: Any, actual_amount: Any) -> dict:
    """The actual of a line (what was bought): recorded on an approved RAB without making it a draft again."""
    amount = price_of(actual_amount)
    aq = float(actual_qty) if actual_qty not in (None, "") else None
    with db.transaction() as conn:
        if not conn.execute(f"""UPDATE {P}.building_rab_line SET actual_qty = %s, actual_amount = %s, updated_by = %s, updated_at = now()
                                WHERE id = %s AND project_id::text = %s""", (aq, amount, _by(user), line_id, pid)).rowcount:
            raise LookupError("Baris RAB tidak ditemukan")
    return full(pid)


def delete_line(user: dict, pid: str, line_id: int) -> dict:
    with db.transaction() as conn:
        _rab_open(conn, pid, user)
        conn.execute(f"DELETE FROM {P}.building_rab_line WHERE id = %s AND project_id::text = %s", (line_id, pid))
    return full(pid)


def rab_submit(user: dict, pid: str) -> dict:
    with db.transaction() as conn:
        row = conn.execute(f"SELECT rab_status FROM {P}.building_project WHERE id::text = %s FOR UPDATE", (pid,)).fetchone()
        if row is None:
            raise LookupError("Proyek tidak ditemukan")
        if row["rab_status"] != "DRAFT":
            raise BuildingError("RAB sudah dikirim / disetujui")
        if not conn.execute(f"SELECT 1 FROM {P}.building_rab_line WHERE project_id::text = %s LIMIT 1", (pid,)).fetchone():
            raise BuildingError("RAB masih kosong")
        conn.execute(f"UPDATE {P}.building_project SET rab_status = 'SUBMITTED', updated_at = now(), updated_by = %s WHERE id::text = %s", (_by(user), pid))
    return full(pid)


def rab_decide(user: dict, pid: str, approve: bool, note: Optional[str]) -> dict:
    """Approve: the RAB is kept as the next version. Return: back to draft with a note."""
    p = full(pid)
    if p["rabStatus"] != "SUBMITTED":
        raise BuildingError("RAB tidak sedang menunggu approval")
    if not approve and not (note or "").strip():
        raise BuildingError("Isi alasan pengembalian")
    with db.transaction() as conn:
        if approve:
            version = p["rabVersion"] + 1
            data = {"sections": p["sections"], "lines": p["lines"], "kinds": p["kinds"]}
            import json
            conn.execute(f"""INSERT INTO {P}.building_rab_version (project_id, version, total, data, approved_by, note)
                             VALUES (%s, %s, %s, %s::jsonb, %s, %s)""", (pid, version, p["rabTotal"], json.dumps(data, default=str), _by(user), _text(note, 500)))
            conn.execute(f"""UPDATE {P}.building_project SET rab_status = 'APPROVED', rab_version = %s, updated_at = now(), updated_by = %s
                             WHERE id::text = %s""", (version, _by(user), pid))
        else:
            conn.execute(f"""UPDATE {P}.building_project SET rab_status = 'DRAFT', updated_at = now(), updated_by = %s,
                                    note = concat_ws(E'\\n', note, %s::text) WHERE id::text = %s""",
                         (_by(user), f"[RAB dikembalikan {_today():%d/%m/%Y}] {note.strip()}", pid))
    return full(pid)


def rab_version(pid: str, version: int) -> dict:
    r = db.fetchrow(f"SELECT * FROM {P}.building_rab_version WHERE project_id::text = %s AND version = %s", (pid, version))
    if r is None:
        raise LookupError("Versi RAB tidak ditemukan")
    return {"version": r["version"], "total": _f(r["total"]), "by": r["approved_by"], "at": _iso(r["approved_at"]), "note": r["note"], **r["data"]}


# ---------------------------------------------------------------- stages & progress

def save_stages(user: dict, pid: str, stages: list[dict]) -> dict:
    """Replaces the stages (name, weight, plan dates); the progress of a kept stage stays. The weights must make 100%."""
    if not stages:
        raise BuildingError("Minimal satu tahapan")
    total = sum(float(s.get("weight") or 0) for s in stages)
    if abs(total - 100) > 0.01:
        raise BuildingError(f"Total bobot harus 100% (sekarang {total:g}%)")
    with db.transaction() as conn:
        _row(pid)
        keep = [int(s["id"]) for s in stages if s.get("id")]
        conn.execute(f"DELETE FROM {P}.building_stage WHERE project_id::text = %s AND NOT (id = ANY(%s))", (pid, keep))
        for i, s in enumerate(stages):
            name = _text(s.get("name"), 120)
            if not name:
                raise BuildingError("Nama tahapan wajib diisi")
            a, b = _date(s.get("planStart")), _date(s.get("planEnd"))
            if a and b and b < a:
                raise BuildingError(f"Tahapan {name}: tanggal selesai sebelum mulai")
            if s.get("id"):
                conn.execute(f"""UPDATE {P}.building_stage SET sort = %s, name = %s, weight = %s, plan_start = %s, plan_end = %s
                                 WHERE id = %s AND project_id::text = %s""", (i, name, s.get("weight") or 0, a, b, int(s["id"]), pid))
            else:
                conn.execute(f"""INSERT INTO {P}.building_stage (project_id, sort, name, weight, plan_start, plan_end)
                                 VALUES (%s, %s, %s, %s, %s, %s)""", (pid, i, name, s.get("weight") or 0, a, b))
    return full(pid)


def report(user: dict, pid: str, stage_id: int, progress: float, note: Optional[str], reported_on: Optional[str], photos: list[str]) -> dict:
    from app.damaged_equipment import media_root
    from app.pic_checkin import parse_photo
    if not 0 <= progress <= 100:
        raise BuildingError("Progress 0 – 100%")
    if len(photos) > MAX_PHOTOS:
        raise BuildingError(f"Maksimal {MAX_PHOTOS} foto")
    day = _date(reported_on) or _today()
    if day > _today():
        raise BuildingError("Tanggal laporan tidak boleh setelah hari ini")
    parsed = [parse_photo(p, shrink=True) for p in photos]
    with db.transaction() as conn:
        row = conn.execute(f"SELECT status FROM {P}.building_project WHERE id::text = %s", (pid,)).fetchone()
        if row is None:
            raise LookupError("Proyek tidak ditemukan")
        if row["status"] in ("DONE", "CANCELLED"):
            raise BuildingError("Proyek sudah selesai / dibatalkan")
        if not conn.execute(f"SELECT 1 FROM {P}.building_stage WHERE id = %s AND project_id::text = %s", (stage_id, pid)).fetchone():
            raise BuildingError("Pilih tahapan")
        gid = conn.execute(f"""INSERT INTO {P}.building_progress (project_id, stage_id, progress, note, reported_on, created_by)
                               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""", (pid, stage_id, progress, _text(note, 2000), day, _by(user))).fetchone()["id"]
        conn.execute(f"""UPDATE {P}.building_stage SET progress = %s, done_on = CASE WHEN %s >= 100 THEN coalesce(done_on, %s) ELSE NULL END
                         WHERE id = %s""", (progress, progress, day, stage_id))
        for mime, data in parsed:
            fid = str(uuid.uuid4())
            rel = f"building/{day:%Y/%m}/{fid}.{'jpg' if mime == 'image/jpeg' else mime.split('/')[-1]}"
            path = media_root() / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            conn.execute(f"INSERT INTO {P}.building_photo (id, progress_id, path, mime) VALUES (%s, %s, %s, %s)", (fid, gid, rel, mime))
    return full(pid)


def photo(photo_id: str) -> Optional[dict]:
    from app.damaged_equipment import media_root
    r = db.fetchrow(f"SELECT path, mime FROM {P}.building_photo WHERE id::text = %s", (photo_id,))
    return {"path": media_root() / r["path"], "mime": r["mime"]} if r else None
