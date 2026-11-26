"""SO Daily auto-submit (2026-11-26, Operations): at 07:00 WIB every SO Daily (DAILY_PACKAGING) still open after its window
is submitted as it is, so a count filled and forgotten reaches Cost Control. A line left empty stays empty: it is not
counted (the stock readers take the lines with a quantity only), never read as 0. Such a count has no signature and no
position; its note says it was submitted automatically and how many lines were empty.

A count with no line filled is not submitted (nothing to send): its day is noted as missing, as an outlet that did no
SO Daily at all. A day is an SO day when at least half of the outlets that count regularly counted on it (a day without
SO Daily, e.g. 21 Nov, notes nobody). Counts older than LOOK_BACK days are left alone.
An outlet that did no SO Daily at all gets no count (an empty count would read as stock 0): the day is kept in
so_daily_missing (migration 158) and the outlet, its PIC and Cost Control are told in the bell.
"""
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app import database as db

P = "integration_portal"
WIB = ZoneInfo("Asia/Jakarta")
AT = time(7, 0)
TYPE = "DAILY_PACKAGING"
LOOK_BACK = 3
# the counts of the outlets: those of HUB WH / HUB CK have their own rhythm
PLACE = """coalesce(b.branch_code, f.code)"""
JOINS = """JOIN kc_master.facility f ON f.id = c.facility_id
           LEFT JOIN kc_master.facility_esb_map m ON m.facility_id = c.facility_id
           LEFT JOIN integration_esb.master_branches b ON b.company_code = m.company_code AND b.branch_id::text = m.esb_id"""


def due(now: datetime) -> bool:
    return now.astimezone(WIB).time() >= AT


def note_of(empty: int, total: int) -> str:
    """The note of a count submitted automatically (pure, tested)."""
    tail = f"; {empty} dari {total} baris belum diisi (tidak dihitung)" if empty else ""
    return f"[Auto-submit 07:00 WIB: tidak di-submit outlet{tail}]"


def submit_open(now: Optional[datetime] = None) -> list[dict]:
    """Submits the SO Daily still open after their window; [{number, place, day, empty, total}]."""
    now = now or datetime.now(WIB)
    if not due(now):
        return []
    rows = db.fetch(
        f"""SELECT c.id::text, c.number, c.business_date AS day, {PLACE} AS place, f.name,
                   (SELECT count(*) FROM kc_inventory.stock_count_line l WHERE l.count_id = c.id AND l.is_current) AS total,
                   (SELECT count(*) FROM kc_inventory.stock_count_line l WHERE l.count_id = c.id AND l.is_current
                       AND l.qty_base IS NULL AND coalesce(l.qty_wip, 0) = 0) AS empty
              FROM kc_inventory.stock_count c {JOINS}
             WHERE c.count_type = %s AND c.status = 'IN_PROGRESS' AND c.window_end_at < %s
               AND c.business_date < %s AND c.business_date >= %s""",
        (TYPE, now, now.astimezone(WIB).date(), now.astimezone(WIB).date() - timedelta(days=LOOK_BACK)))
    done = []
    with db.transaction() as conn:
        for r in rows:
            if int(r["empty"]) >= int(r["total"]):  # nothing filled: noted as missing, not sent
                continue
            n = conn.execute(
                """UPDATE kc_inventory.stock_count SET status = 'SUBMITTED', submitted_at = now(), is_late = true,
                          note = trim(coalesce(note, '') || ' ' || %s), updated_at = now()
                    WHERE id::text = %s AND status = 'IN_PROGRESS'""", (note_of(int(r["empty"]), int(r["total"])), r["id"])).rowcount
            if n:
                done.append({"id": r["id"], "number": r["number"], "place": r["place"], "name": r["name"], "day": r["day"],
                             "empty": int(r["empty"]), "total": int(r["total"])})
    return done


def is_so_day(regular: int, counted: int) -> bool:
    """A day of SO Daily: at least half of the outlets that count regularly counted on it (pure, tested)."""
    return regular > 0 and counted * 2 >= regular


def missing(day: date) -> list[dict]:
    """The outlets that did an SO Daily in the 7 days before `day` but none on it (or one with no line filled): [{place, name}].
    None on a day that is no SO day."""
    regular = db.fetch(
        f"""SELECT DISTINCT c.facility_id, {PLACE} AS place, f.name FROM kc_inventory.stock_count c {JOINS}
             WHERE c.count_type = %s AND c.status <> 'CANCELLED' AND c.business_date BETWEEN %s AND %s AND {PLACE} IS NOT NULL""",
        (TYPE, day - timedelta(days=7), day - timedelta(days=1)))
    done = {r["facility_id"] for r in db.fetch(
        """SELECT c.facility_id FROM kc_inventory.stock_count c
            WHERE c.count_type = %s AND c.business_date = %s AND c.status <> 'CANCELLED'
              AND EXISTS (SELECT 1 FROM kc_inventory.stock_count_line l WHERE l.count_id = c.id AND l.is_current
                          AND (l.qty_base IS NOT NULL OR coalesce(l.qty_wip, 0) > 0))""", (TYPE, day))}
    if not is_so_day(len({r["facility_id"] for r in regular}), len(done & {r["facility_id"] for r in regular})):
        return []
    seen: dict[str, dict] = {}
    for r in regular:
        if r["facility_id"] not in done:
            seen.setdefault(r["place"], {"place": r["place"], "name": r["name"]})
    return list(seen.values())


def record_missing(now: Optional[datetime] = None) -> list[dict]:
    """At 07:00 the SO Daily of yesterday that never started: kept once per outlet and day."""
    now = now or datetime.now(WIB)
    if not due(now):
        return []
    day = now.astimezone(WIB).date() - timedelta(days=1)
    out = []
    with db.transaction() as conn:
        for r in missing(day):
            got = conn.execute(f"""INSERT INTO {P}.so_daily_missing (day, branch_code, name) VALUES (%s, %s, %s)
                                   ON CONFLICT (day, branch_code) DO NOTHING RETURNING day""", (day, r["place"], r["name"])).fetchone()
            if got:
                out.append({**r, "day": day})
    return out


def run(now: Optional[datetime] = None) -> list[dict]:
    """The step of the alert loop: the counts submitted and the days missing, as notices for the bell."""
    sent, gone = submit_open(now), record_missing(now)
    notes = []
    for r in sent:
        body = f"{r['name']} · {r['number']}" + (f" · {r['empty']} dari {r['total']} baris belum diisi" if r["empty"] else "")
        notes.append({"dedup": f"so_auto:{r['id']}", "kind": "so_auto", "severity": "warn", "place": r["place"], "doc": r["number"],
                      "title": f"SO Daily {r['day']:%d/%m/%Y} di-submit otomatis 07:00", "body": body, "link": f"/stock-opname/{r['id']}"})
    for r in gone:
        notes.append({"dedup": f"so_missing:{r['place']}:{r['day']}", "kind": "so_missing", "severity": "high", "place": r["place"], "doc": None,
                      "title": f"Belum melakukan SO Daily {r['day']:%d/%m/%Y}", "body": r["name"], "link": None})
    return notes
