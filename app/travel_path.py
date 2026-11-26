"""Travel Path (2026-11-25, migration 153; SOP Travel Path — Foto Checklist Outlet Rev. 3): the crew of an outlet walks its
zones at every session of the day (opening, 15.00, closing) and proves every area with a photo.

Areas      app/travel_def.py, changed in Branch Groups → Form Items like Product and Outlet (form "travel"): name, photo
           points, standard, category, order, active, per branch group and per outlet.
Answer     OK (Sesuai: photo), NOK (Tidak sesuai: photo and what was found) or NA (not at this outlet: the reason).
Report     one per outlet, business day (outlet's clock) and session, shared by the accounts of the outlet. Sent after the
           window of its session = late; sent more than `reportMinutes` after the last photo = report late.
Score      OK ÷ (OK + NOK); one critical finding caps it at `criticalCap`. Bands: standard / needs repair / not passed.
Findings   every NOK, with a deadline by its category (hours, or before the next session); closed with an after photo, in
           the report itself or later; the monitor may open it again with a note.
Settings   sessions (window, branch groups), score and deadlines: Branch Groups → Travel Path."""
import copy
import json
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app import database as db
from app import visit_forms as vf
from app.pic_checkin import CheckInError, branch_options, judge, locations, parse_photo

P = "integration_portal"
T, TF, TP, TS = f"{P}.travel_path", f"{P}.travel_path_finding", f"{P}.travel_path_photo", f"{P}.travel_path_setting"
FORM = "travel"
RATINGS = ("OK", "NOK", "NA")
CRITICAL = "Kritikal"
MAX_PHOTOS = 5
WIB = ZoneInfo("Asia/Jakarta")
DEFAULTS: dict = {
    "since": "2026-12-01", "dayEnds": "04:00", "reportMinutes": 30, "needSupervisor": True,
    "sessions": [
        {"key": "opening", "label": "Opening", "labelEn": "Opening", "from": "06:00", "to": "11:00", "active": True, "groups": []},
        {"key": "afternoon", "label": "Pukul 15.00", "labelEn": "3 PM", "from": "15:00", "to": "17:00", "active": True, "groups": []},
        {"key": "closing", "label": "Closing", "labelEn": "Closing", "from": "20:00", "to": "02:00", "active": True, "groups": []},
    ],
    "criticalCap": 70, "standard": 95, "pass": 85,
    # hours to repair per category; None = before the next Travel Path
    "due": {"Kritikal": 8, "Mayor": 24, "Minor": None},
    "escalation": {"Kritikal": "Head Operasional", "Mayor": "Area Supervisor", "Minor": "Supervisor Outlet"},
}


class TravelError(CheckInError):
    """Refused with a message for the user (HTTP 422)."""


# ---------------------------------------------------------------- settings

def settings() -> dict:
    row = None
    try:
        row = db.fetchrow(f"SELECT config FROM {TS} WHERE id = 1")
    except Exception:  # noqa: BLE001 - before migration 153
        pass
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in ((row or {}).get("config") or {}).items() if k in DEFAULTS})
    return out


def _clock(v: Any, what: str) -> str:
    try:
        return datetime.strptime(str(v), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise TravelError(f"Jam {what} harus HH:MM") from None


def clean_settings(data: dict, groups: set[str]) -> dict:
    """The settings checked. Pure (tested)."""
    sessions, keys = [], set()
    for s in data.get("sessions") or []:
        label = " ".join(str(s.get("label") or "").split())[:40]
        key = str(s.get("key") or "").strip().lower()[:30]
        if not label or not key or not key.replace("-", "").isalnum():
            raise TravelError("Setiap sesi perlu nama")
        if key in keys:
            raise TravelError(f"Sesi {label} ganda")
        keys.add(key)
        a, z = _clock(s.get("from"), label), _clock(s.get("to"), label)
        if a == z:
            raise TravelError(f"Jam mulai dan selesai {label} tidak boleh sama")
        sessions.append({"key": key, "label": label, "labelEn": " ".join(str(s.get("labelEn") or label).split())[:40], "from": a, "to": z,
                         "active": bool(s.get("active", True)), "groups": sorted({g for g in s.get("groups") or [] if g in groups})})
    if not any(s["active"] for s in sessions):
        raise TravelError("Minimal satu sesi aktif")

    def num(k: str, lo: float, hi: float) -> float:
        try:
            v = float(data.get(k))
        except (TypeError, ValueError):
            raise TravelError(f"{k} harus angka") from None
        if not lo <= v <= hi:
            raise TravelError(f"{k} harus {lo:g} sampai {hi:g}")
        return v
    out = {"sessions": sessions, "dayEnds": _clock(data.get("dayEnds"), "ganti hari"), "reportMinutes": int(num("reportMinutes", 1, 1440)),
           "needSupervisor": bool(data.get("needSupervisor")), "criticalCap": num("criticalCap", 0, 100), "standard": num("standard", 0, 100),
           "pass": num("pass", 0, 100)}
    if out["pass"] > out["standard"]:
        raise TravelError("Batas lolos tidak boleh di atas batas standar")
    try:
        out["since"] = date.fromisoformat(str(data.get("since"))).isoformat()
    except ValueError:
        raise TravelError("Tanggal mulai tidak valid") from None
    due = {}
    for c, h in (data.get("due") or {}).items():
        c = str(c).strip()[:40]
        if not c:
            continue
        if h is None:
            due[c] = None
            continue
        if not 0 < float(h) <= 24 * 60:
            raise TravelError(f"Batas perbaikan {c} harus lebih dari 0 jam")
        due[c] = float(h)
    out["due"] = due
    out["escalation"] = {str(c).strip()[:40]: str(v or "").strip()[:80] for c, v in (data.get("escalation") or {}).items() if str(c).strip()}
    return out


def save_settings(data: dict, actor: Optional[str]) -> dict:
    groups = {r["code"] for r in db.fetch(f"SELECT code FROM {P}.branch_group")}
    out = clean_settings(data, groups)
    with db.transaction() as conn:
        conn.execute(f"""INSERT INTO {TS} (id, config, updated_by) VALUES (1, %s::jsonb, %s)
                         ON CONFLICT (id) DO UPDATE SET config = EXCLUDED.config, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                     (json.dumps(out), actor))
    return out


# ---------------------------------------------------------------- pure rules (tested)

def _t(v: str) -> time:
    return time.fromisoformat(v)


def business_day(now: datetime, zone: ZoneInfo, day_ends: str) -> date:
    """The outlet's day: what happens before `day_ends` still belongs to the day before."""
    local = now.astimezone(zone)
    return local.date() - timedelta(days=1 if local.time() < _t(day_ends) else 0)


def window(s: dict, day: date, zone: ZoneInfo, day_ends: str) -> tuple[datetime, datetime]:
    """From / to of a session on a business day (a time before `day_ends` is on the next calendar day)."""
    def at(v: str) -> datetime:
        x = _t(v)
        return datetime.combine(day + timedelta(days=1 if x < _t(day_ends) else 0), x, tzinfo=zone)
    a, z = at(s["from"]), at(s["to"])
    return a, z if z > a else z + timedelta(days=1)


def sessions_of(cfg: dict, group: Optional[str]) -> list[dict]:
    """The sessions of an outlet (of a branch group; no group listed = every outlet)."""
    return [s for s in cfg["sessions"] if s["active"] and (not s["groups"] or (group in s["groups"]))]


def due_at(cfg: dict, category: Optional[str], found: datetime, nexts: list[datetime]) -> datetime:
    """When a finding must be repaired. `nexts`: the starts of the sessions to come."""
    h = cfg["due"].get(category or "", 24)
    if h is not None:
        return found + timedelta(hours=float(h))
    later = [x for x in nexts if x > found]
    return min(later) if later else found + timedelta(hours=24)


def score_of(items: list[dict], answers: dict, cfg: dict) -> dict:
    """counts, score and band of a report over the areas asked."""
    ok = nok = na = critical = 0
    for it in items:
        r = (answers.get(it["key"]) or {}).get("rating")
        if r == "OK":
            ok += 1
        elif r == "NOK":
            nok += 1
            critical += it.get("category") == CRITICAL
        elif r == "NA":
            na += 1
    score = round(ok * 100 / (ok + nok), 1) if ok + nok else None
    if score is not None and critical:
        score = min(score, float(cfg["criticalCap"]))
    band = None if score is None else "standard" if score >= cfg["standard"] else "repair" if score >= cfg["pass"] else "failed"
    return {"ok": ok, "nok": nok, "na": na, "critical": critical, "done": ok + nok + na, "total": len(items), "score": score, "band": band}


def gaps(items: list[dict], answers: dict, photos: list[dict]) -> list[dict]:
    """What blocks the sending: an area not answered (unless optional), NA without its reason, NOK without what was found, or
    OK / NOK without the photo it asks for."""
    main = {p["item"] for p in photos if p["kind"] == "MAIN"}
    out = []
    for it in items:
        a = answers.get(it["key"]) or {}
        r = a.get("rating")
        need = []
        if not r:
            if it.get("required", True):
                need.append("rating")
        else:
            if r in ("NOK", "NA") and len(a.get("note") or "") < 3:
                need.append("note")
            elif r == "OK" and it.get("note") == "required" and not a.get("note"):
                need.append("note")
            if r != "NA" and it["key"] not in main and (it.get("photo", "required") == "required" or r == "NOK"):
                need.append("photo")
        if need:
            out.append({"key": it["key"], "label": it["label"], "labelEn": it.get("labelEn"), "need": need})
    return out


SHARE_BASE = os.getenv("PORTAL_PUBLIC_URL", "https://portal.kopicalf.co.id").rstrip("/")
BAND_TEXT = {"standard": "Standar", "repair": "Perlu perbaikan", "failed": "Tidak lulus"}


def share_url(token: Optional[str]) -> Optional[str]:
    return f"{SHARE_BASE}/share/travel/{token}" if token else None


def evaluation(r: dict, items: list[dict], cfg: dict) -> list[str]:
    """The automatic evaluation of a sent report (pure, tested): the band against the thresholds, the critical findings,
    the zones with the most findings, what was repaired at once, the timing and the place."""
    c = r.get("counts") or {}
    a = r.get("answers") or {}
    out = []
    score = c.get("score")
    if score is None:
        out.append("Belum ada area yang dinilai.")
    elif score >= cfg["standard"]:
        out.append(f"Skor {score:g}% memenuhi standar ({cfg['standard']:g}%).")
    elif score >= cfg["pass"]:
        out.append(f"Skor {score:g}% di bawah standar {cfg['standard']:g}%: perlu perbaikan.")
    else:
        out.append(f"Skor {score:g}% di bawah batas lulus {cfg['pass']:g}%: tidak lulus.")
    nok = [it for it in items if (a.get(it["key"]) or {}).get("rating") == "NOK"]
    crit = [it for it in nok if it.get("category") == CRITICAL]
    if crit:
        out.append(f"{len(crit)} temuan Kritikal ({', '.join(it['label'] for it in crit[:3])}); skor dibatasi {cfg['criticalCap']:g}%.")
    zones: dict[str, int] = {}
    for it in nok:
        zones[it.get("section_title") or "-"] = zones.get(it.get("section_title") or "-", 0) + 1
    if zones:
        worst = sorted(zones.items(), key=lambda x: -x[1])[:2]
        out.append("Temuan terbanyak: " + ", ".join(f"{z} ({n})" for z, n in worst) + ".")
    fixed = sum(1 for it in nok if (a.get(it["key"]) or {}).get("fix"))
    if nok:
        out.append(f"{fixed} dari {len(nok)} temuan langsung diperbaiki; sisanya wajib ditutup dengan foto after sebelum tenggat.")
    if not nok and score is not None:
        out.append("Tidak ada temuan.")
    if r.get("late"):
        out.append("Dikirim setelah jam sesi berakhir.")
    if r.get("report_late"):
        out.append(f"Dikirim lebih dari {cfg['reportMinutes']} menit setelah foto terakhir.")
    if r.get("outside_radius"):
        out.append(f"Dikirim di luar radius outlet ({r.get('distance_m') or '?'} m).")
    return out


def report_text(r: dict, items: list[dict], cfg: dict) -> str:
    """The message of the SOP for the group chat, with the automatic evaluation and the link to every photo."""
    at = (r["submitted_at"] or r["updated_at"]).astimezone(WIB)
    c = r["counts"] or {}
    a = r["answers"] or {}
    na = [it["label"] for it in items if (a.get(it["key"]) or {}).get("rating") == "NA"]
    found = [(it, a[it["key"]]) for it in items if (a.get(it["key"]) or {}).get("rating") == "NOK"]
    band = f" · {BAND_TEXT.get(r.get('band'), '')}" if r.get("band") else ""
    lines = [f"*TRAVEL PATH — {r['branch_name']}*", f"Sesi           : {r['session_label']}", f"Tanggal / jam  : {at:%d/%m/%Y} · {at:%H.%M} WIB",
             f"Pelaksana      : {' & '.join(x for x in (r['crew'], r['supervisor']) if x)}",
             f"Area diperiksa : {c.get('done', 0)} / {c.get('total', 0)}  ·  N/A: {', '.join(na) or '-'}",
             (f"Skor           : *{c['score']:g}%*{band}" if c.get("score") is not None else "Skor           : -"), "Temuan         :" + ("" if found else " -")]
    for n, (it, x) in enumerate(found, start=1):
        fix = f" — diperbaiki: {x['fix']}" if x.get("fix") else ""
        lines.append(f"  {n}. [{it.get('category') or '-'}] {it['label']}: {x.get('note') or '-'}{fix}")
    lines.append("")
    lines.append("*Evaluasi*")
    lines += [f"• {e}" for e in evaluation(r, items, cfg)]
    url = share_url(r.get("share_token"))
    if url:
        lines += ["", f"Lampiran foto: {url}"]
    lines.append("cc " + " ".join("@" + v for v in dict.fromkeys(cfg["escalation"].values()) if v))
    return "\n".join(lines)


# ---------------------------------------------------------------- the report

COLS = f"""r.id::text, r.branch_code, r.branch_name, r.branch_group, r.day, r.session, r.session_label, r.status, r.crew, r.supervisor,
    r.started_name, r.started_at, r.updated_at, r.submitted_name, r.submitted_at, r.window_end, r.late, r.report_late, r.answers, r.counts, r.score,
    r.band, r.distance_m, r.outside_radius, r.share_token, (SELECT count(*) FROM {TP} p WHERE p.report_id = r.id)::int AS photo_count"""


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def _view(r: dict) -> dict:
    return {"id": r["id"], "branchCode": r["branch_code"], "branchName": r["branch_name"], "branchGroup": r["branch_group"], "day": r["day"].isoformat(),
            "session": r["session"], "sessionLabel": r["session_label"], "status": r["status"], "crew": r["crew"], "supervisor": r["supervisor"],
            "startedBy": r["started_name"], "startedAt": _iso(r["started_at"]), "updatedAt": _iso(r["updated_at"]), "submittedBy": r["submitted_name"],
            "submittedAt": _iso(r["submitted_at"]), "windowEnd": _iso(r["window_end"]), "late": r["late"], "reportLate": r["report_late"],
            "counts": r["counts"], "score": float(r["score"]) if r["score"] is not None else None, "band": r["band"],
            "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"], "photoCount": r["photo_count"], "shareUrl": share_url(r.get("share_token"))}


def _row(report_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {COLS} FROM {T} r WHERE r.id::text = %s", (report_id,))


def items_of(group: Optional[str], branch: str) -> list[dict]:
    """The areas this outlet is asked, in the order of the route."""
    return [it for it in vf.all_items(FORM) if vf.applies(it, group, branch)]


def definition_of(group: Optional[str], branch: str) -> dict:
    d = copy.deepcopy(vf.definition(FORM))
    for g in d["groups"]:
        for s in g["sections"]:
            s["items"] = [it for it in s["items"] if vf.applies(it, group, branch)]
        g["sections"] = [s for s in g["sections"] if s["items"]]
    return d


def photos_of(report_id: str) -> list[dict]:
    return [{"id": r["id"], "item": r["item_key"], "kind": r["kind"], "takenAt": _iso(r["taken_at"]), "uploadedAt": _iso(r["uploaded_at"]),
             "distanceM": r["distance_m"], "outsideRadius": r["outside_radius"]} for r in db.fetch(
        f"""SELECT id::text, item_key, kind, taken_at, uploaded_at, distance_m, outside_radius FROM {TP}
            WHERE report_id::text = %s ORDER BY uploaded_at""", (report_id,))]


def _finding(f: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"id": f["id"], "reportId": f["report_id"], "branchCode": f["branch_code"], "branchName": f["branch_name"], "day": f["day"].isoformat(),
            "session": f["session"], "itemKey": f["item_key"], "label": f["label"], "zone": f["zone"], "category": f["category"], "note": f["note"],
            "dueAt": _iso(f["due_at"]), "status": f["status"], "overdue": f["status"] == "OPEN" and bool(f["due_at"]) and f["due_at"] < now,
            "fixedLate": f["status"] == "FIXED" and bool(f["due_at"]) and f["fixed_at"] > f["due_at"], "fixNote": f["fix_note"], "fixedBy": f["fixed_by"],
            "fixedAt": _iso(f["fixed_at"]), "reopenedNote": f["reopened_note"], "reopenedBy": f["reopened_by"], "reopenedAt": _iso(f["reopened_at"]),
            "createdAt": _iso(f["created_at"]), "photos": f.get("photos") or {}}


FCOLS = f"""f.id::text, f.report_id::text, f.branch_code, f.branch_name, f.day, f.session, f.item_key, f.label, f.zone, f.category, f.note, f.due_at,
    f.status, f.fix_note, f.fixed_by, f.fixed_at, f.reopened_note, f.reopened_by, f.reopened_at, f.created_at,
    (SELECT jsonb_object_agg(x.kind, x.ids) FROM (SELECT p.kind, jsonb_agg(p.id::text ORDER BY p.uploaded_at) AS ids FROM {TP} p
        WHERE p.report_id = f.report_id AND p.item_key = f.item_key GROUP BY p.kind) x) AS photos"""


def findings(where: str, args: tuple, limit: int = 1000) -> list[dict]:
    return [_finding(f) for f in db.fetch(f"SELECT {FCOLS} FROM {TF} f WHERE {where} ORDER BY f.status DESC, f.due_at, f.created_at LIMIT {int(limit)}", args)]


def full(report_id: str) -> dict:
    r = _row(report_id)
    if r is None:
        raise LookupError("Laporan Travel Path tidak ditemukan")
    cfg = settings()
    items = items_of(r["branch_group"], r["branch_code"])
    out = {**_view(r), "answers": r["answers"] or {}, "photos": photos_of(report_id), "definition": definition_of(r["branch_group"], r["branch_code"]),
           "needSupervisor": cfg["needSupervisor"]}
    if r["status"] == "DRAFT":
        out["counts"] = score_of(items, out["answers"], cfg)
    else:
        out["findings"] = findings("f.report_id::text = %s", (report_id,))
        out["text"] = report_text(r, items, cfg)
        out["evaluation"] = evaluation(r, items, cfg)
    return out


def shared(token: str) -> Optional[dict]:
    """A sent report opened from its link (no sign-in): no ids of people, no place."""
    row = db.fetchrow(f"SELECT r.id::text FROM {T} r WHERE r.share_token = %s AND r.status = 'SUBMITTED'", (token,))
    if not row:
        return None
    out = full(row["id"])
    for k in ("id", "distanceM", "startedBy", "submittedBy", "shareUrl"):
        out.pop(k, None)
    out["photos"] = [{k: v for k, v in p.items() if k not in ("distanceM",)} for p in out["photos"]]
    return out


def shared_photo(token: str, photo_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT p.photo, p.photo_type FROM {TP} p JOIN {T} r ON r.id = p.report_id
                           WHERE r.share_token = %s AND r.status = 'SUBMITTED' AND p.id::text = %s""", (token, photo_id))


def outlets_for(user: dict) -> list[dict]:
    """The outlets of the account that do the Travel Path: Kopi Calf outlets, no HUB / CK (the test branches DMY-… too)."""
    from app import branch_groups
    codes = {b["code"] for b in branch_groups.outlets()}
    return [b for b in branch_options(user) if b["code"] in codes or b["code"].startswith("DMY-")]


def _outlet_of(user: dict, code: str) -> dict:
    b = next((b for b in outlets_for(user) if b["code"] == code), None)
    if b is None:
        raise TravelError("Outlet tidak tersedia untuk akun Anda")
    return b


def zone_of(code: str) -> ZoneInfo:
    from app import pic_settings
    try:
        return pic_settings.deadlines([code])[code]["zone"]
    except Exception:  # noqa: BLE001 - no setting for the outlet
        return WIB


def today_sessions(branch: dict, cfg: dict, now: datetime) -> tuple[date, list[dict]]:
    """The sessions of the outlet's day with their window and report. state: done / draft / open / late / early."""
    zone = zone_of(branch["code"])
    day = business_day(now, zone, cfg["dayEnds"])
    rows = {r["session"]: r for r in db.fetch(f"SELECT {COLS} FROM {T} r WHERE r.branch_code = %s AND r.day = %s", (branch["code"], day))}
    out = []
    for s in sessions_of(cfg, branch.get("group")):
        a, z = window(s, day, zone, cfg["dayEnds"])
        r = rows.get(s["key"])
        state = "done" if r and r["status"] == "SUBMITTED" else "early" if now < a and not r else "late" if now > z else "open"
        out.append({"key": s["key"], "label": s["label"], "labelEn": s["labelEn"], "from": a.isoformat(), "to": z.isoformat(),
                    "fromText": a.strftime("%H:%M"), "toText": z.strftime("%H:%M"), "state": state, "report": _view(r) if r else None})
    return day, out


def form_state(user: dict, branch: Optional[str]) -> dict:
    cfg = settings()
    branches = outlets_for(user)
    pick = next((b for b in branches if b["code"] == branch), branches[0] if len(branches) == 1 else None)
    out: dict = {"branches": [{"code": b["code"], "name": b["name"], "group": b.get("group")} for b in branches], "branch": pick["code"] if pick else None,
                 "settings": {k: cfg[k] for k in ("reportMinutes", "criticalCap", "standard", "pass", "due", "escalation", "needSupervisor")}}
    if pick:
        day, ss = today_sessions(pick, cfg, datetime.now(timezone.utc))
        out.update(day=day.isoformat(), sessions=ss, areas=len(items_of(pick.get("group"), pick["code"])),
                   recent=[_view(r) for r in db.fetch(f"""SELECT {COLS} FROM {T} r WHERE r.branch_code = %s AND r.status = 'SUBMITTED'
                                                          ORDER BY r.submitted_at DESC LIMIT 12""", (pick["code"],))],
                   findings=findings("f.branch_code = %s AND f.status = 'OPEN'", (pick["code"],), 200))
    return out


def open_report(user: dict, branch_code: str, session: str) -> dict:
    """The report of a session of today, started when there is none."""
    b = _outlet_of(user, branch_code)
    cfg = settings()
    day, ss = today_sessions(b, cfg, datetime.now(timezone.utc))
    s = next((x for x in ss if x["key"] == session), None)
    if s is None:
        raise TravelError("Sesi Travel Path tidak tersedia untuk outlet ini")
    if s["report"]:
        return full(s["report"]["id"])
    if s["state"] == "early":
        raise TravelError(f"Travel Path {s['label']} dibuka pukul {s['fromText']}")
    row = db.fetchrow(
        f"""INSERT INTO {T} (branch_code, branch_name, branch_group, day, session, session_label, started_by, started_name, window_end)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (branch_code, day, session) DO UPDATE SET updated_at = {T.split('.')[1]}.updated_at RETURNING id::text""",
        (b["code"], b["name"], b.get("group"), day, s["key"], s["label"], user["id"], user.get("full_name") or user["username"], s["to"]))
    return full(row["id"])


def _mine(user: dict, report_id: str, draft: bool = True) -> dict:
    r = _row(report_id)
    if r is None or r["branch_code"] not in {b["code"] for b in outlets_for(user)}:
        raise LookupError("Laporan Travel Path tidak ditemukan")
    if draft and r["status"] != "DRAFT":
        raise TravelError("Laporan ini sudah dikirim")
    return r


def get_report(user: dict, report_id: str) -> dict:
    _mine(user, report_id, draft=False)
    return full(report_id)


def save(user: dict, report_id: str, patch: dict) -> dict:
    """{answers: {area: {rating?, note?, fix?}}, crew?, supervisor?} merged into the draft (several phones may fill it)."""
    r = _mine(user, report_id)
    items = items_of(r["branch_group"], r["branch_code"])
    known = {it["key"] for it in items}
    cfg = settings()
    with db.transaction() as conn:
        row = conn.execute(f"SELECT answers, status FROM {T} WHERE id::text = %s FOR UPDATE", (report_id,)).fetchone()
        if row["status"] != "DRAFT":
            raise TravelError("Laporan ini sudah dikirim")
        answers = dict(row["answers"] or {})
        for key, v in (patch.get("answers") or {}).items():
            if key not in known or not isinstance(v, dict):
                raise TravelError(f"Area tidak dikenal: {key}")
            cur = dict(answers.get(key) or {})
            if "rating" in v:
                if v["rating"] is not None and v["rating"] not in RATINGS:
                    raise TravelError("Penilaian harus Sesuai, Tidak sesuai atau N/A")
                cur["rating"] = v["rating"]
            for f in ("note", "fix"):
                if f in v:
                    cur[f] = str(v[f] or "").strip()[:1000] or None
            cur["by"] = user.get("full_name") or user["username"]
            cur = {k: x for k, x in cur.items() if x is not None}
            answers[key] = cur
        c = score_of(items, answers, cfg)
        sets, args = ["answers = %s::jsonb", "counts = %s::jsonb", "updated_at = now()"], [json.dumps(answers), json.dumps(c)]
        for f in ("crew", "supervisor"):
            if f in patch:
                sets.append(f"{f} = %s")
                args.append(" ".join(str(patch[f] or "").split())[:200] or None)
        conn.execute(f"UPDATE {T} SET {', '.join(sets)} WHERE id::text = %s", (*args, report_id))
    return {"answers": answers, "counts": c}


def _store_photo(r: dict, item: str, kind: str, user: dict, photo: str, lat: Optional[float], lng: Optional[float], taken: Optional[datetime], conn=None) -> None:
    q = conn.execute if conn is not None else None
    sql = f"SELECT count(*)::int AS n FROM {TP} WHERE report_id::text = %s AND item_key = %s AND kind = %s"
    n = (q(sql, (r["id"], item, kind)).fetchone() if q else db.fetchrow(sql, (r["id"], item, kind)))["n"]
    if n >= MAX_PHOTOS:
        raise TravelError(f"Maksimal {MAX_PHOTOS} foto per area; hapus satu dulu")
    ptype, data = parse_photo(photo, shrink=True)
    outlet = locations([r["branch_code"]]).get(r["branch_code"])
    j = judge(lat, lng, None, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    ins = f"""INSERT INTO {TP} (report_id, item_key, kind, photo, photo_type, taken_at, latitude, longitude, distance_m, outside_radius, uploaded_by)
              VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"""
    args = (r["id"], item, kind, data, ptype, taken, lat, lng, j["distance_m"], j["outside_radius"], user["id"])
    if q:
        q(ins, args)
    else:
        db.fetchrow(ins, args)


def add_photo(user: dict, report_id: str, item: str, kind: str, photo: str, lat: Optional[float], lng: Optional[float], taken: Optional[datetime]) -> dict:
    r = _mine(user, report_id)
    if item not in {it["key"] for it in items_of(r["branch_group"], r["branch_code"])}:
        raise TravelError(f"Area tidak dikenal: {item}")
    _store_photo(r, item, kind, user, photo, lat, lng, taken)
    return {"photos": photos_of(report_id)}


def delete_photo(user: dict, report_id: str, photo_id: str) -> dict:
    _mine(user, report_id)
    db.fetchrow(f"DELETE FROM {TP} WHERE id::text = %s AND report_id::text = %s RETURNING id", (photo_id, report_id))
    return {"photos": photos_of(report_id)}


def discard(user: dict, report_id: str) -> None:
    _mine(user, report_id)
    db.fetchrow(f"DELETE FROM {T} WHERE id::text = %s AND status = 'DRAFT' RETURNING id", (report_id,))


def submit(user: dict, report_id: str, lat: Optional[float], lng: Optional[float], accuracy: Optional[float]) -> dict:
    r = _mine(user, report_id)
    cfg = settings()
    items = items_of(r["branch_group"], r["branch_code"])
    answers, photos = r["answers"] or {}, photos_of(report_id)
    if not r["crew"]:
        raise TravelError("Tulis nama crew pelaksana")
    if cfg["needSupervisor"] and not r["supervisor"]:
        raise TravelError("Tulis nama Supervisor Outlet yang mendampingi")
    todo = gaps(items, answers, photos)
    if todo:
        raise TravelError(f"Belum bisa dikirim: {len(todo)} area belum lengkap ({todo[0]['label']})")
    c = score_of(items, answers, cfg)
    if not c["ok"] + c["nok"]:
        raise TravelError("Belum ada area yang dinilai")
    now = datetime.now(timezone.utc)
    taken = [datetime.fromisoformat(p["uploadedAt"]) for p in photos if p["kind"] == "MAIN"]
    late = bool(r["window_end"]) and now > r["window_end"]
    report_late = bool(taken) and now - max(taken) > timedelta(minutes=cfg["reportMinutes"])
    outlet = locations([r["branch_code"]]).get(r["branch_code"])
    j = judge(lat, lng, accuracy, outlet) if lat is not None and lng is not None else {"distance_m": None, "outside_radius": False}
    zone, name = zone_of(r["branch_code"]), user.get("full_name") or user["username"]
    nexts = [window(s, r["day"] + timedelta(days=d), zone, cfg["dayEnds"])[0] for d in (0, 1) for s in sessions_of(cfg, r["branch_group"])]
    after = {p["item"] for p in photos if p["kind"] == "AFTER"}
    with db.transaction() as conn:
        row = conn.execute(
            f"""UPDATE {T} SET status = 'SUBMITTED', submitted_at = now(), submitted_by = %s, submitted_name = %s, updated_at = now(), counts = %s::jsonb,
                    score = %s, band = %s, late = %s, report_late = %s, share_token = %s, latitude = %s, longitude = %s, accuracy_m = %s, distance_m = %s, outside_radius = %s
                WHERE id::text = %s AND status = 'DRAFT' RETURNING id""",
            (user["id"], name, json.dumps(c), c["score"], c["band"], late, report_late, secrets.token_urlsafe(16), lat, lng, accuracy, j["distance_m"], j["outside_radius"],
             report_id)).fetchone()
        if row is None:
            raise TravelError("Laporan ini sudah dikirim")
        for it in items:
            a = answers.get(it["key"]) or {}
            if a.get("rating") != "NOK":
                continue
            fixed = it["key"] in after  # repaired at once, the after photo in the same report
            conn.execute(
                f"""INSERT INTO {TF} (report_id, branch_code, branch_name, day, session, item_key, label, zone, category, note, due_at, status, fix_note,
                                      fixed_by, fixed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)""",
                (report_id, r["branch_code"], r["branch_name"], r["day"], r["session"], it["key"], it["label"], it["section_title"], it.get("category"),
                 a.get("note"), due_at(cfg, it.get("category"), now, nexts), "FIXED" if fixed else "OPEN", a.get("fix"), name if fixed else None, fixed))
    return full(report_id)


# ---------------------------------------------------------------- findings

def fix(user: dict, finding_id: str, note: str, photo: str, lat: Optional[float], lng: Optional[float], taken: Optional[datetime]) -> dict:
    """The outlet closes a finding: what was done and the after photo."""
    note = (note or "").strip()[:1000]
    if len(note) < 3:
        raise TravelError("Tulis perbaikan yang dilakukan")
    f = db.fetchrow(f"SELECT report_id::text AS report, branch_code, item_key, status FROM {TF} WHERE id::text = %s", (finding_id,))
    if f is None or f["branch_code"] not in {b["code"] for b in outlets_for(user)}:
        raise LookupError("Temuan tidak ditemukan")
    with db.transaction() as conn:
        got = conn.execute(f"""UPDATE {TF} SET status = 'FIXED', fix_note = %s, fixed_by = %s, fixed_at = now()
                               WHERE id::text = %s AND status = 'OPEN' RETURNING id""",
                           (note, user.get("full_name") or user["username"], finding_id)).fetchone()
        if got is None:
            raise TravelError("Temuan ini sudah selesai")
        _store_photo({"id": f["report"], "branch_code": f["branch_code"]}, f["item_key"], "AFTER", user, photo, lat, lng, taken, conn)
    return findings("f.id::text = %s", (finding_id,))[0]


def reopen(user: dict, finding_id: str, note: str, branches: Optional[tuple]) -> dict:
    """The monitor opens a finding again: the repair is not good enough."""
    note = (note or "").strip()[:500]
    if len(note) < 3:
        raise TravelError("Tulis alasan dibuka lagi")
    f = db.fetchrow(f"SELECT branch_code, status FROM {TF} WHERE id::text = %s", (finding_id,))
    if f is None or (branches is not None and f["branch_code"] not in branches):
        raise LookupError("Temuan tidak ditemukan")
    if f["status"] != "FIXED":
        raise TravelError("Temuan ini masih terbuka")
    db.fetchrow(f"""UPDATE {TF} SET status = 'OPEN', reopened_note = %s, reopened_by = %s, reopened_at = now() WHERE id::text = %s RETURNING id""",
                (note, user.get("full_name") or user["username"], finding_id))
    return findings("f.id::text = %s", (finding_id,))[0]


def photo(photo_id: str) -> Optional[dict]:
    return db.fetchrow(f"""SELECT p.photo, p.photo_type, r.branch_code, r.status FROM {TP} p JOIN {T} r ON r.id = p.report_id WHERE p.id::text = %s""", (photo_id,))


# ---------------------------------------------------------------- monitoring

def compliance(outlets: list[dict], reports: dict, days: list[date], cfg: dict, now: datetime, zones: dict) -> tuple[list[dict], dict]:
    """Per outlet and day the cell of every session, and the totals. Pure (tested). state: done / late (sent after the window) /
    missing (the window is over) / waiting. Sessions before `since` are not expected."""
    since = date.fromisoformat(cfg["since"])
    rows, tot = [], {"expected": 0, "done": 0, "late": 0, "missing": 0}
    for o in outlets:
        cells, exp, done, scores = [], 0, 0, []
        zone = zones.get(o["code"], WIB)
        for d in days:
            day_cells = []
            for s in sessions_of(cfg, o.get("group")):
                r = reports.get((o["code"], d, s["key"]))
                end = window(s, d, zone, cfg["dayEnds"])[1]
                state = ("late" if r["late"] else "done") if r else "missing" if now > end else "waiting"
                if d < since and not r:
                    state = "none"
                if state in ("done", "late", "missing"):
                    exp += 1
                if r:
                    done += 1
                    if r["score"] is not None:
                        scores.append(float(r["score"]))
                tot["late"] += state == "late"
                tot["missing"] += state == "missing"
                day_cells.append({"session": s["key"], "state": state, "id": r["id"] if r else None,
                                  "score": float(r["score"]) if r and r["score"] is not None else None, "critical": (r["counts"] or {}).get("critical", 0) if r else 0})
            cells.append({"date": d.isoformat(), "sessions": day_cells})
        tot["expected"] += exp
        tot["done"] += done
        rows.append({"code": o["code"], "name": o["name"], "group": o.get("group"), "days": cells, "expected": exp, "done": done, "missing": max(0, exp - done),
                     "rate": round(done * 100 / exp, 1) if exp else None, "score": round(sum(scores) / len(scores), 1) if scores else None})
    tot["rate"] = round(tot["done"] * 100 / tot["expected"], 1) if tot["expected"] else None
    return rows, tot


def monitor(date_from: date, date_to: date, branches: Optional[list[str]]) -> dict:
    from app import branch_groups, pic_settings
    cfg = settings()
    now = datetime.now(timezone.utc)
    outs = [{"code": b["code"], "name": b["name"], "group": b["group_code"]} for b in branch_groups.outlets()]
    if branches is not None:
        outs = [o for o in outs if o["code"] in set(branches)]
    codes = [o["code"] for o in outs]
    p = {"a": date_from, "b": date_to, "br": codes}
    rows = db.fetch(f"""SELECT {COLS} FROM {T} r WHERE r.status = 'SUBMITTED' AND r.day BETWEEN %(a)s AND %(b)s AND r.branch_code = ANY(%(br)s)
                        ORDER BY r.submitted_at DESC LIMIT 5000""", p)
    # only the outlets open in the period (sales from 14 days before it) or with a report
    alive = {r["branch_code"] for r in db.fetch(f"""SELECT DISTINCT branch_code FROM {P}.agg_sales_daily
                                                    WHERE sales_date BETWEEN %(a)s::date - 14 AND %(b)s AND branch_code = ANY(%(br)s)""", p)}
    alive |= {r["branch_code"] for r in rows}
    outs = [o for o in outs if o["code"] in alive]
    last = min(date_to, business_day(now, WIB, cfg["dayEnds"]))
    days = [date_from + timedelta(days=i) for i in range((last - date_from).days + 1)] if last >= date_from else []
    try:
        zones = {c: s["zone"] for c, s in pic_settings.deadlines([o["code"] for o in outs]).items()}
    except Exception:  # noqa: BLE001
        zones = {}
    per, tot = compliance(outs, {(r["branch_code"], r["day"], r["session"]): r for r in rows}, days, cfg, now, zones)
    fs = findings("f.day BETWEEN %s AND %s AND f.branch_code = ANY(%s)", (date_from, date_to, codes), 3000)
    # findings still open from before the period
    old = findings("f.status = 'OPEN' AND f.day < %s AND f.branch_code = ANY(%s)", (date_from, codes), 1000)
    top: dict[str, dict] = {}
    for f in fs:
        x = top.setdefault(f["itemKey"], {"key": f["itemKey"], "label": f["label"], "zone": f["zone"], "category": f["category"], "count": 0, "outlets": set(), "open": 0})
        x["count"] += 1
        x["outlets"].add(f["branchName"])
        x["open"] += f["status"] == "OPEN"
    scores = [float(r["score"]) for r in rows if r["score"] is not None]
    grp: dict[str, dict] = {}
    for o in per:
        g = grp.setdefault(o["group"] or "—", {"code": o["group"] or "—", "outlets": 0, "expected": 0, "done": 0, "scores": []})
        g["outlets"] += 1
        g["expected"] += o["expected"]
        g["done"] += o["done"]
        if o["score"] is not None:
            g["scores"].append(o["score"])
    return {"sessions": [{"key": s["key"], "label": s["label"], "labelEn": s["labelEn"], "from": s["from"], "to": s["to"]} for s in cfg["sessions"] if s["active"]],
            "since": cfg["since"], "criticalCap": cfg["criticalCap"], "standard": cfg["standard"], "pass": cfg["pass"],
            "dates": [d.isoformat() for d in days], "outlets": sorted(per, key=lambda x: (-x["missing"], x["name"])), "reports": [_view(r) for r in rows],
            "findings": old + fs, "truncated": len(rows) >= 5000,
            "top": sorted(({**x, "outlets": sorted(x["outlets"])} for x in top.values()), key=lambda x: -x["count"]),
            "groups": [{"code": g["code"], "outlets": g["outlets"], "expected": g["expected"], "done": g["done"],
                        "rate": round(g["done"] * 100 / g["expected"], 1) if g["expected"] else None,
                        "score": round(sum(g["scores"]) / len(g["scores"]), 1) if g["scores"] else None} for g in sorted(grp.values(), key=lambda g: (g["code"] == "—", g["code"]))],
            "summary": {**tot, "outlets": len(outs), "reports": len(rows), "score": round(sum(scores) / len(scores), 1) if scores else None,
                        "failed": sum(1 for r in rows if r["band"] == "failed"), "critical": sum(1 for f in fs if f["category"] == CRITICAL),
                        "openFindings": sum(1 for f in old + fs if f["status"] == "OPEN"), "overdue": sum(1 for f in old + fs if f["overdue"])}}


# ---------------------------------------------------------------- notices (app/alerts.py)

def notices(rows: list[dict], people: dict[str, dict[str, list[str]]]) -> list[dict]:
    """A critical finding is told to the PICs of the outlet, an overdue one to the outlet and its PICs. Pure (tested).
    people = branch: {"outlet": [users], "pic": [users]}."""
    out = []
    for f in rows:
        who = people.get(f["branchCode"], {})
        if f["overdue"]:
            users, kind, title = [*who.get("outlet", []), *who.get("pic", [])], "travel_overdue", f"Temuan Travel Path lewat batas: {f['label']}"
        elif f["category"] == CRITICAL and f["status"] == "OPEN":
            users, kind, title = who.get("pic", []), "travel_critical", f"Temuan kritikal Travel Path: {f['label']}"
        else:
            continue
        for u in users:
            out.append({"dedup": f"{kind}:{f['id']}:{f['reopenedAt'] or ''}:{u}", "kind": kind, "severity": "high", "doc": None, "branch": f["branchCode"], "user": u,
                        "title": title, "body": f"{f['branchName']}: {f['note'] or '-'}", "link": "/forms/travel" if u in who.get("outlet", []) else None})
    return out


def scan() -> list[dict]:
    from app.alerts import PIC_PLACES
    rows = findings("f.status = 'OPEN' AND f.created_at > now() - interval '7 days'", (), 2000)
    codes = sorted({f["branchCode"] for f in rows if f["overdue"] or f["category"] == CRITICAL})
    people: dict[str, dict[str, list[str]]] = {}
    if codes:
        for r in db.fetch(f"""SELECT ub.branch_code AS b, u.id::text AS u, u.role FROM {P}.user_branch ub JOIN {P}.user_account u ON u.id = ub.user_id
                               WHERE u.is_active AND ub.branch_code = ANY(%s) AND (u.role = 'outlet' OR (u.role = 'user' AND
                                     (SELECT count(*) FROM {P}.user_branch x WHERE x.user_id = u.id) <= %s))""", (codes, PIC_PLACES)):
            people.setdefault(r["b"], {}).setdefault("outlet" if r["role"] == "outlet" else "pic", []).append(r["u"])
    return notices(rows, people)
