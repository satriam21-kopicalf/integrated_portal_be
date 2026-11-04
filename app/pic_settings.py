"""The settings of the PIC assessment (2026-11-04, migration 069), kept in Monitoring OPS → Branch groups:

* the check-in deadline: the first check-in of the PIC's day is on time when it is made by the deadline of its outlet —
  the outlet's own deadline, else its branch group's (CCI 10:00, CTGP 10:15, CTG 10:30), else DEFAULT_DEADLINE — read on
  the outlet's own clock (an outlet in WITA, e.g. Renon Bali, is on time until 10:30 WITA = 09:30 WIB). A check-in
  before EARLIEST (05:00 local) is not on time (the workbook counted check-ins after midnight as punctual);
* the monthly net sales target of every outlet (the sales part of the PIC score).

The working days of a period follow the 6-1 schedule of the PICs (six days on, one free day of their choice):
working_days(n) = n − ⌊n / 7⌋, so a month of 28 / 29 / 30 / 31 days holds 24 / 25 / 26 / 27 working days (30 → 26, as
the workbook)."""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from app import branch_groups as bg
from app import database as db

TS = "integration_portal.outlet_setting"
TT = "integration_portal.outlet_sales_target"   # migration 069; unused since 2026-11-04 (net sales against the outlet itself)
DEFAULT_DEADLINE = time(11, 0)     # an outlet without a group nor its own deadline (the workbook's 11:00)
EARLIEST = time(5, 0)
ZONES = {"Asia/Jakarta": "WIB", "Asia/Makassar": "WITA", "Asia/Jayapura": "WIT"}


class SettingError(ValueError):
    """A change refused with a message for the user (HTTP 422)."""


def working_days(days: int) -> int:
    """Working days in a span of `days` calendar days under the 6-1 schedule."""
    return max(0, days - days // 7)


def _hhmm(t: Optional[time]) -> Optional[str]:
    return t.strftime("%H:%M") if t else None


def _time(v: Optional[str]) -> Optional[time]:
    if v in (None, ""):
        return None
    try:
        h, m = str(v).split(":")[:2]
        t = time(int(h), int(m))
    except (ValueError, TypeError):
        raise SettingError("Jam harus HH:MM, mis. 10:30") from None
    if not (time(5, 0) <= t <= time(14, 0)):
        raise SettingError("Batas check in antara 05:00 dan 14:00")
    return t


# ---------------------------------------------------------------- deadlines

def deadlines(codes: list[str]) -> dict[str, dict]:
    """{branch code: {deadline: time, zone: ZoneInfo, source: outlet|group|default}} of the outlets."""
    if not codes:
        return {}
    rows = db.fetch(f"""SELECT c AS code, s.checkin_deadline AS own, s.timezone, g.checkin_deadline AS grp
                        FROM unnest(%s::text[]) c
                        LEFT JOIN {TS} s ON s.branch_code = c
                        LEFT JOIN integration_portal.branch_group_member m ON m.branch_code = c
                        LEFT JOIN integration_portal.branch_group g ON g.id = m.group_id""", (codes,))
    out = {}
    for r in rows:
        source = "outlet" if r["own"] else "group" if r["grp"] else "default"
        out[r["code"]] = {"deadline": r["own"] or r["grp"] or DEFAULT_DEADLINE, "zone": ZoneInfo(r["timezone"] or "Asia/Jakarta"), "source": source}
    for c in codes:
        out.setdefault(c, {"deadline": DEFAULT_DEADLINE, "zone": ZoneInfo("Asia/Jakarta"), "source": "default"})
    return out


def deadline_labels(codes: list[str]) -> dict[str, dict]:
    """{branch code: {deadline: "HH:MM", zone: "WITA", wib: "HH:MM"}}: the deadline as the PIC sees it in Operations."""
    out = {}
    for code, s in deadlines(codes).items():
        local = datetime.combine(date.today(), s["deadline"], tzinfo=s["zone"])
        out[code] = {"deadline": s["deadline"].strftime("%H:%M"), "zone": ZONES.get(str(s["zone"]), "WIB"),
                     "wib": local.astimezone(ZoneInfo("Asia/Jakarta")).strftime("%H:%M")}
    return out


def on_time(at: datetime, setting: dict) -> bool:
    """Is a check-in at `at` (aware) on time for an outlet with this setting (its own clock)?"""
    local = at.astimezone(setting["zone"]).time()
    return EARLIEST <= local <= setting["deadline"]


def outlet_settings() -> dict:
    """Every Kopi Calf outlet with its group, the group's deadline, its own deadline and time zone."""
    outs = bg.outlets()
    rows = {r["branch_code"]: r for r in db.fetch(f"SELECT branch_code, checkin_deadline, timezone FROM {TS}")}
    groups = {g["id"]: g for g in db.fetch("SELECT id::text, code, checkin_deadline FROM integration_portal.branch_group")}
    data = []
    for o in sorted(outs, key=lambda x: x["name"]):
        s = rows.get(o["code"], {})
        g = groups.get(o["group_id"] or "")
        data.append({"code": o["code"], "name": o["name"], "groupCode": o["group_code"],
                     "groupDeadline": _hhmm(g["checkin_deadline"]) if g else None,
                     "deadline": _hhmm(s.get("checkin_deadline")), "timezone": s.get("timezone") or "Asia/Jakarta",
                     "zone": ZONES.get(s.get("timezone") or "Asia/Jakarta", "WIB")})
    return {"data": data, "defaultDeadline": _hhmm(DEFAULT_DEADLINE), "zones": [{"value": k, "label": v} for k, v in ZONES.items()]}


def save_outlet(code: str, deadline: Optional[str], timezone: Optional[str], actor: Optional[str]) -> dict:
    if code not in {o["code"] for o in bg.outlets()}:
        raise LookupError("Outlet tidak ditemukan")
    t = _time(deadline)
    tz = timezone or "Asia/Jakarta"
    if tz not in ZONES:
        raise SettingError("Zona waktu WIB, WITA atau WIT")
    with db.transaction() as conn:
        if t is None and tz == "Asia/Jakarta":
            conn.execute(f"DELETE FROM {TS} WHERE branch_code = %s", (code,))
        else:
            conn.execute(f"""INSERT INTO {TS} (branch_code, checkin_deadline, timezone, updated_by) VALUES (%s, %s, %s, %s)
                             ON CONFLICT (branch_code) DO UPDATE SET checkin_deadline = EXCLUDED.checkin_deadline, timezone = EXCLUDED.timezone,
                             updated_at = now(), updated_by = EXCLUDED.updated_by""", (code, t, None if tz == "Asia/Jakarta" else tz, actor))
    return {"code": code, "deadline": _hhmm(t), "timezone": tz}


def group_deadline(value: Optional[str]) -> Optional[time]:
    return _time(value)


# ---------------------------------------------------------------- net sales (Sales Transactions of the outlet)
# No target is set by hand (user decision 2026-11-04): an outlet is compared with itself. Its net sales per selling day
# in the period against its net sales per selling day over the BASELINE_MONTHS full months before the period. Both
# come from the Sales Transactions (ESB) aggregates, so a big and a small outlet are judged alike.

BASELINE_MONTHS = 1          # the month before only (user decision 2026-11-04; was 3)


def _month(v: str) -> date:
    try:
        y, m = (int(x) for x in str(v)[:7].split("-"))
        return date(y, m, 1)
    except (ValueError, TypeError):
        raise SettingError("Bulan harus YYYY-MM") from None


def _months_back(d: date, n: int) -> date:
    y, m = d.year, d.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _baseline_window(start: date) -> tuple[date, date]:
    first = date(start.year, start.month, 1)
    return _months_back(first, BASELINE_MONTHS), first - timedelta(days=1)


def daily_sales(codes: list[str], start: date, end: date) -> dict[str, tuple[Decimal, int]]:
    """{branch code: (net sales of the sales bills from start to end, days with sales)} (the Dashboard aggregates)."""
    if not codes or start > end:
        return {}
    rows = db.fetch("""SELECT branch_code, sum(nett_sales) AS n, count(DISTINCT sales_date) FILTER (WHERE nett_sales > 0) AS d
                       FROM integration_portal.agg_sales_daily
                       WHERE tx_type = 'sales' AND sales_date BETWEEN %s AND %s AND branch_code = ANY(%s) GROUP BY branch_code""",
                    (start, end, codes))
    return {r["branch_code"]: (Decimal(r["n"] or 0), int(r["d"] or 0)) for r in rows}


def _per_day(v: Optional[tuple[Decimal, int]]) -> Optional[Decimal]:
    return v[0] / v[1] if v and v[1] and v[0] > 0 else None


def sales_scores(codes: list[str], start: date, end: date) -> dict[str, Optional[float]]:
    """{branch code: net sales per selling day of the period against its own per day of the BASELINE_MONTHS before, %,
    at most 100}. None for an outlet without sales in the period or in the baseline (a new outlet)."""
    if not codes:
        return {}
    end = min(end, date.today())
    a, b = _baseline_window(start)
    now, base = daily_sales(codes, start, end), daily_sales(codes, a, b)
    out = {}
    for c in codes:
        n, m = _per_day(now.get(c)), _per_day(base.get(c))
        out[c] = round(min(100.0, float(n * 100 / m)), 1) if n is not None and m else None
    return out


def sales_overview(month: str) -> dict:
    """Every outlet: its net sales of the month so far and per selling day against its own per day of the months before."""
    m = _month(month)
    last = date(m.year, m.month, monthrange(m.year, m.month)[1])
    upto = min(last, date.today())
    outs = sorted(bg.outlets(), key=lambda x: x["name"])
    codes = [o["code"] for o in outs]
    a, b = _baseline_window(m)
    now, base = daily_sales(codes, m, upto), daily_sales(codes, a, b)
    data = []
    for o in outs:
        n, bm = now.get(o["code"]), base.get(o["code"])
        pn, pb = _per_day(n), _per_day(bm)
        data.append({"code": o["code"], "name": o["name"], "groupCode": o["group_code"],
                     "netSales": float(n[0]) if n else 0.0, "days": n[1] if n else 0,
                     "perDay": float(pn) if pn is not None else None, "baselinePerDay": float(pb) if pb is not None else None,
                     "vsBaseline": round(float(pn * 100 / pb), 1) if pn is not None and pb else None,
                     "score": round(min(100.0, float(pn * 100 / pb)), 1) if pn is not None and pb else None})
    return {"month": m.isoformat()[:7], "baselineFrom": a.isoformat(), "baselineTo": b.isoformat(),
            "baselineMonths": BASELINE_MONTHS, "upTo": upto.isoformat() if upto >= m else None, "data": data}
