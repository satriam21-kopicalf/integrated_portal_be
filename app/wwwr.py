"""Form WW-WR (2026-11-03, migration 064): the field audit of an outlet by the SWAT team, every item of Monitoring
Product and Monitoring Outlet in one visit (app/visit_forms.py, form "wwwr", keys "product:…" / "outlet:…").

The management schedules an audit (Monitoring WW-WR → Schedule): an outlet (any Kopi Calf branch), a day, a lead and
the members (accounts with the page form.wwwr). One audit per outlet per day. From that day the members see it in
Operations → Form WW-WR and fill the one shared visit (each answer keeps who filled it); the lead sends it with the
signature. Nobody outside the team opens it.

The comparison (Monitoring WW-WR → Comparison) sets every sent audit against the latest Monitoring Product and
Monitoring Outlet visits the PIC sent for that outlet in the COMPARE_DAYS before it: per item the rating of the PIC and
of the SWAT team; "agree" when both pass (MEET / EXCEED) or both fail (UNDER), "overrated" when the PIC passed what the
team found UNDER, "underrated" the other way. Conformity = agree / items rated by both — material to evaluate the PICs.
"""
from datetime import date, timedelta
from typing import Any, Optional

from app import database as db
from app import visit_forms as vf
from app.pic_checkin import branch_options

P = "integration_portal"
COMPARE_DAYS = 7
PASS = {"MEET", "EXCEED"}


class AuditError(vf.VisitError):
    """A schedule refused with a message (HTTP 422)."""


# ---------------------------------------------------------------- team and schedule

def team() -> list[dict]:
    """Active accounts with the page form.wwwr (the SWAT team)."""
    from app import access
    users = db.fetch(f"""SELECT id, username, full_name, role FROM {P}.user_account
                          WHERE is_active AND role <> 'superadmin' ORDER BY coalesce(full_name, username)""")
    return [{"id": str(u["id"]), "username": u["username"], "name": u["full_name"] or u["username"]}
            for u in users if access.has_page(u, "form.wwwr")]


def _status(a: dict) -> str:
    if a["status"] == "cancelled":
        return "cancelled"
    if a.get("visit_status") == "SUBMITTED":
        return "submitted"
    return "in_progress" if a.get("visit_status") == "DRAFT" else "scheduled"


def _audits(where: str, params: tuple) -> list[dict]:
    rows = db.fetch(f"""SELECT a.*, a.id::text AS aid, a.lead_id::text AS lead, a.visit_id::text AS vid, v.status AS visit_status,
                               v.submitted_at, v.counts, v.score, coalesce(l.full_name, l.username) AS lead_name,
                               (SELECT json_agg(json_build_object('id', u.id::text, 'name', coalesce(u.full_name, u.username),
                                                                  'username', u.username) ORDER BY coalesce(u.full_name, u.username))
                                  FROM {P}.wwwr_audit_member m JOIN {P}.user_account u ON u.id = m.user_id WHERE m.audit_id = a.id) AS members
                          FROM {P}.wwwr_audit a
                          LEFT JOIN {P}.visit_form v ON v.id = a.visit_id
                          LEFT JOIN {P}.user_account l ON l.id = a.lead_id
                         WHERE {where} ORDER BY a.day DESC, a.branch_name""", params)
    return [{"id": r["aid"], "branchCode": r["branch_code"], "branchName": r["branch_name"], "day": r["day"].isoformat(),
             "lead": {"id": r["lead"], "name": r["lead_name"]}, "members": r["members"] or [], "note": r["note"],
             "status": _status(r), "visitId": r["vid"],
             "submittedAt": r["submitted_at"].isoformat() if r["submitted_at"] else None,
             "counts": r["counts"], "score": float(r["score"]) if r["score"] is not None else None} for r in rows]


def schedule(d_from: date, d_to: date, branches: Optional[list[str]]) -> list[dict]:
    if branches is None:
        return _audits("a.day BETWEEN %s AND %s", (d_from, d_to))
    return _audits("a.day BETWEEN %s AND %s AND a.branch_code = ANY(%s)", (d_from, d_to, branches))


def _branch(code: str) -> dict:
    b = next((b for b in branch_options({}, every=True) if b["code"] == code), None)
    if b is None:
        raise AuditError("Outlet tidak ditemukan")
    return b


def save_audit(user: dict, audit_id: Optional[str], data: dict) -> dict:
    """Create (audit_id None) or change a scheduled audit: outlet, day, lead, members (the lead is always one)."""
    b = _branch(str(data.get("branchCode") or ""))
    day = data.get("day")
    if not isinstance(day, date):
        raise AuditError("Tanggal audit wajib diisi")
    members = {str(m) for m in (data.get("memberIds") or [])}
    lead = str(data.get("leadId") or "")
    allowed = {t["id"] for t in team()}
    if not lead or lead not in allowed:
        raise AuditError("Pilih ketua tim dari anggota SWAT (akun dengan halaman Form WW-WR)")
    members.add(lead)
    if members - allowed:
        raise AuditError("Anggota tim harus akun dengan halaman Form WW-WR")
    note = (str(data.get("note") or "")).strip()[:500] or None
    try:
        with db.transaction() as conn:
            if audit_id is None:
                row = conn.execute(f"""INSERT INTO {P}.wwwr_audit (branch_code, branch_name, day, lead_id, note, created_by)
                                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id::text""",
                                   (b["code"], b["name"], day, lead, note, user["id"])).fetchone()
                audit_id = row["id"]
            else:
                cur = conn.execute(f"""SELECT a.status, v.status AS visit_status, a.visit_id FROM {P}.wwwr_audit a
                                         LEFT JOIN {P}.visit_form v ON v.id = a.visit_id WHERE a.id::text = %s FOR UPDATE OF a""",
                                   (audit_id,)).fetchone()
                if not cur:
                    raise AuditError("Jadwal audit tidak ditemukan")
                if cur["status"] == "cancelled" or cur["visit_status"] == "SUBMITTED":
                    raise AuditError("Audit yang sudah dikirim atau dibatalkan tidak dapat diubah")
                if cur["visit_id"] and data.get("branchCode") and conn.execute(
                        f"SELECT 1 FROM {P}.visit_form WHERE id = %s AND branch_code <> %s", (cur["visit_id"], b["code"])).fetchone():
                    raise AuditError("Audit sudah mulai diisi; outlet tidak dapat diganti")
                conn.execute(f"""UPDATE {P}.wwwr_audit SET branch_code = %s, branch_name = %s, day = %s, lead_id = %s, note = %s,
                                        updated_at = now() WHERE id::text = %s""", (b["code"], b["name"], day, lead, note, audit_id))
                if cur["visit_id"]:  # the visit belongs to the lead
                    conn.execute(f"""UPDATE {P}.visit_form v SET user_id = u.id, username = u.username, full_name = u.full_name
                                       FROM {P}.user_account u WHERE v.id = %s AND u.id::text = %s""", (cur["visit_id"], lead))
                conn.execute(f"DELETE FROM {P}.wwwr_audit_member WHERE audit_id::text = %s", (audit_id,))
            for m in sorted(members):
                conn.execute(f"INSERT INTO {P}.wwwr_audit_member (audit_id, user_id) VALUES (%s, %s)", (audit_id, m))
    except AuditError:
        raise
    except Exception as e:  # noqa: BLE001 - the one-per-day index
        if "wwwr_audit_one_per_day" in str(e):
            raise AuditError(f"{b['name']} sudah dijadwalkan audit pada tanggal itu") from None
        raise
    return _audits("a.id::text = %s", (audit_id,))[0]


def cancel_audit(audit_id: str) -> None:
    with db.transaction() as conn:
        cur = conn.execute(f"""SELECT v.status AS visit_status, a.visit_id FROM {P}.wwwr_audit a LEFT JOIN {P}.visit_form v ON v.id = a.visit_id
                                WHERE a.id::text = %s AND a.status <> 'cancelled' FOR UPDATE OF a""", (audit_id,)).fetchone()
        if not cur:
            raise AuditError("Jadwal audit tidak ditemukan")
        if cur["visit_status"] == "SUBMITTED":
            raise AuditError("Audit sudah dikirim dan tidak dapat dibatalkan")
        conn.execute(f"UPDATE {P}.wwwr_audit SET status = 'cancelled', visit_id = NULL, updated_at = now() WHERE id::text = %s", (audit_id,))
        if cur["visit_id"]:
            conn.execute(f"DELETE FROM {P}.visit_form WHERE id = %s AND status = 'DRAFT'", (cur["visit_id"],))


# ---------------------------------------------------------------- the SWAT member

def state(user: dict) -> dict:
    """Operations → Form WW-WR: the audits of the member (not cancelled), today and upcoming first, then the last sent."""
    uid = str(user["id"])
    mine = "a.status <> 'cancelled' AND EXISTS (SELECT 1 FROM integration_portal.wwwr_audit_member m WHERE m.audit_id = a.id AND m.user_id::text = %s)"
    rows = _audits(f"{mine} AND a.day >= current_date - 30", (uid,))
    for r in rows:
        r.pop("score", None)  # the team never sees a score: it is for the management
        if r["counts"]:
            r["counts"] = {k: v for k, v in r["counts"].items() if k != "score"}
    return {"definition": vf.DEFINITIONS["wwwr"], "user": {"id": uid, "username": user["username"],
            "name": user.get("full_name") or user["username"]}, "audits": rows}


def open_audit(user: dict, audit_id: str) -> dict:
    """The shared visit of an audit, started (in the name of the lead) on its day or later."""
    from app.utils import today
    a = db.fetchrow(f"""SELECT a.*, a.id::text AS aid FROM {P}.wwwr_audit a WHERE a.id::text = %s AND a.status <> 'cancelled'
                          AND EXISTS (SELECT 1 FROM {P}.wwwr_audit_member m WHERE m.audit_id = a.id AND m.user_id::text = %s)""",
                    (audit_id, str(user["id"])))
    if not a:
        raise LookupError("Audit tidak ditemukan atau Anda tidak termasuk tim")
    if a["day"] > today():
        raise vf.VisitError(f"Audit ini dijadwalkan {a['day']:%d/%m/%Y}; dapat dibuka mulai hari itu")
    with db.transaction() as conn:
        a = conn.execute(f"SELECT * FROM {P}.wwwr_audit WHERE id::text = %s FOR UPDATE", (audit_id,)).fetchone()
        if a["visit_id"] is None:
            lead = conn.execute(f"SELECT id, username, full_name, role FROM {P}.user_account WHERE id = %s", (a["lead_id"],)).fetchone()
            if conn.execute(f"""SELECT 1 FROM {P}.visit_form WHERE form = 'wwwr' AND user_id = %s AND branch_code = %s AND status = 'DRAFT'""",
                            (lead["id"], a["branch_code"])).fetchone():
                raise vf.VisitError("Ketua tim masih punya audit WW-WR outlet ini yang belum dikirim")
            v = conn.execute(f"""INSERT INTO {P}.visit_form (form, user_id, username, full_name, role, branch_code, branch_name)
                                 VALUES ('wwwr', %s, %s, %s, %s, %s, %s) RETURNING id""",
                             (lead["id"], lead["username"], lead["full_name"], lead["role"], a["branch_code"], a["branch_name"])).fetchone()
            conn.execute(f"UPDATE {P}.wwwr_audit SET visit_id = %s, updated_at = now() WHERE id = %s", (v["id"], a["id"]))
            visit_id = str(v["id"])
        else:
            visit_id = str(a["visit_id"])
    return with_team(vf.full(visit_id))


def with_team(visit: dict) -> dict:
    """A WW-WR visit with its audit: day, lead and members."""
    a = _audits("a.visit_id::text = %s", (visit["id"],))
    if a:
        visit["audit"] = {k: a[0][k] for k in ("id", "day", "lead", "members", "note", "status")}
    return visit


def teams_of(visit_ids: list[str]) -> dict[str, dict]:
    if not visit_ids:
        return {}
    return {a["visitId"]: {k: a[k] for k in ("id", "day", "lead", "members")}
            for a in _audits("a.visit_id::text = ANY(%s)", (visit_ids,))}


# ---------------------------------------------------------------- comparison with the PIC

def _pic_visit(form: str, branch: str, before: Any) -> Optional[dict]:
    return db.fetchrow(f"""SELECT {vf.COLS} FROM {vf.T} v WHERE v.form = %s AND v.branch_code = %s AND v.status = 'SUBMITTED'
                             AND v.submitted_at <= %s AND v.submitted_at >= %s - interval '{COMPARE_DAYS} days'
                           ORDER BY v.submitted_at DESC LIMIT 1""", (form, branch, before, before))


def _compare(audit: dict, part: str, pic: Optional[dict], with_items: bool = False) -> dict:
    swat = audit["answers"] or {}
    items = [it for it in vf.all_items(part) if it["kind"] == "rating"]
    s_rated = [swat.get(f"{part}:{it['key']}", {}).get("rating") for it in items]
    s_done = [r for r in s_rated if r]
    out: dict[str, Any] = {"part": part, "swatScore": round(sum(1 for r in s_done if r in PASS) * 100 / len(s_done), 1) if s_done else None,
                           "swatUnder": sum(1 for r in s_done if r == "UNDER"), "pic": None}
    if pic is None:
        if with_items:
            out["items"] = [_row(it, part, swat, {}) for it in items]
        return out
    pa = pic["answers"] or {}
    agree = over = under = compared = 0
    rows = []
    for it in items:
        s = swat.get(f"{part}:{it['key']}", {}).get("rating")
        p = pa.get(it["key"], {}).get("rating")
        if s and p:
            compared += 1
            if (s in PASS) == (p in PASS):
                agree += 1
            elif p in PASS:
                over += 1
            else:
                under += 1
        if with_items:
            rows.append(_row(it, part, swat, pa))
    out["pic"] = {"visitId": pic["id"], "userId": pic["user_id"], "name": pic["full_name"] or pic["username"], "username": pic["username"],
                  "submittedAt": pic["submitted_at"].isoformat(), "score": float(pic["score"]) if pic["score"] is not None else None,
                  "compared": compared, "agree": agree, "overrated": over, "underrated": under,
                  "conformity": round(agree * 100 / compared, 1) if compared else None}
    if with_items:
        out["items"] = rows
    return out


def _row(it: dict, part: str, swat: dict, pic: dict) -> dict:
    s = swat.get(f"{part}:{it['key']}", {})
    p = pic.get(it["key"], {})
    sr, pr = s.get("rating"), p.get("rating")
    verdict = None if not (sr and pr) else "agree" if (sr in PASS) == (pr in PASS) else "overrated" if pr in PASS else "underrated"
    return {"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "section": it["section_title"],
            "sectionEn": it["section_title_en"], "swat": sr, "swatNote": s.get("note"), "swatBy": s.get("by"),
            "pic": pr, "picNote": p.get("note"), "verdict": verdict}


def comparison(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    """Every audit sent in the period against the PIC's Product and Outlet visits; the figures per PIC."""
    where = ["v.form = 'wwwr'", "v.status = 'SUBMITTED'", "(v.submitted_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s"]
    params: list[Any] = [d_from, d_to]
    if branches is not None:
        where.append("v.branch_code = ANY(%s)")
        params.append(branches)
    audits = db.fetch(f"SELECT {vf.COLS} FROM {vf.T} v WHERE {' AND '.join(where)} ORDER BY v.submitted_at DESC LIMIT 500", tuple(params))
    teams = teams_of([a["id"] for a in audits])
    out, per = [], {}
    for a in audits:
        parts = [_compare(a, part, _pic_visit(part, a["branch_code"], a["submitted_at"])) for part in vf.PARTS]
        out.append({"visitId": a["id"], "branchCode": a["branch_code"], "branchName": a["branch_name"],
                    "submittedAt": a["submitted_at"].isoformat(), "score": float(a["score"]) if a["score"] is not None else None,
                    "team": teams.get(a["id"]), "parts": parts})
        for p in parts:
            if not p["pic"] or not p["pic"]["compared"]:
                continue
            x = per.setdefault(p["pic"]["userId"], {"id": p["pic"]["userId"], "name": p["pic"]["name"], "audits": 0, "compared": 0,
                                                    "agree": 0, "overrated": 0, "underrated": 0, "branches": set()})
            x["audits"] += 1
            x["branches"].add(a["branch_name"])
            for k in ("compared", "agree", "overrated", "underrated"):
                x[k] += p["pic"][k]
    pics = sorted(({**x, "branches": sorted(x["branches"]), "conformity": round(x["agree"] * 100 / x["compared"], 1)} for x in per.values()),
                  key=lambda x: (x["conformity"], -x["overrated"]))
    compared = sum(x["compared"] for x in pics)
    summary = {"audits": len(out), "withPic": sum(1 for a in out if any(p["pic"] for p in a["parts"])),
               "conformity": round(sum(x["agree"] for x in pics) * 100 / compared, 1) if compared else None,
               "overrated": sum(x["overrated"] for x in pics), "underrated": sum(x["underrated"] for x in pics)}
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "days": COMPARE_DAYS, "summary": summary, "audits": out, "pics": pics}


def comparison_of(visit_id: str) -> Optional[dict]:
    """One audit item by item against the PIC (both parts)."""
    a = vf.get_visit(visit_id)
    if not a or a["form"] != "wwwr" or a["status"] != "SUBMITTED":
        return None
    parts = [_compare(a, part, _pic_visit(part, a["branch_code"], a["submitted_at"]), with_items=True) for part in vf.PARTS]
    return {"visitId": a["id"], "branchCode": a["branch_code"], "branchName": a["branch_name"], "submittedAt": a["submitted_at"].isoformat(),
            "score": float(a["score"]) if a["score"] is not None else None, "team": teams_of([a["id"]]).get(a["id"]), "parts": parts,
            "days": COMPARE_DAYS}
