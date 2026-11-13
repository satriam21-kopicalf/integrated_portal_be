"""Schedule of the Cost Control team (Cost Control · Clock In, 2026-11-13; migration 108).

The leader of Cost Control (an account given the page cc.schedule by a superadmin) plans which outlets each member
visits on which day; the members are the accounts that open Cost Control Clock In (form.surveyor-check-in). A member sees
the outlets of the day in the Clock In form; the plan is set against the clock ins (pic_checkin kind 'surveyor'):

    done      clocked in at that outlet that day
    missed    a day gone by without a clock in there
    planned   today or later, not clocked in yet
A clock in at an outlet not planned that day shows as unplanned.
"""
from datetime import date, timedelta
from typing import Optional

from app import database as db

P = "integration_portal"
T = f"{P}.cc_visit_schedule"
MAX_DAYS = 62


class ScheduleError(ValueError):
    """Refused with a message for the user (HTTP 422)."""


def team() -> list[dict]:
    """The members: active accounts that open Cost Control Clock In (their role or an exception of the account)."""
    from app import access
    rows = db.fetch(f"""SELECT id::text, username, full_name, role FROM {P}.user_account WHERE is_active ORDER BY coalesce(full_name, username)""")
    out = []
    for r in rows:
        if r["role"] == "superadmin":
            continue
        if access.has_page({"id": r["id"], "role": r["role"]}, "form.surveyor-check-in"):
            out.append({"id": r["id"], "username": r["username"], "name": r["full_name"] or r["username"]})
    return out


def status_of(day: date, done: bool, today: date) -> str:
    if done:
        return "done"
    return "missed" if day < today else "planned"


def week(d_from: date, d_to: date) -> dict:
    """The plan of the period with its status, the clock ins outside the plan, the team and the totals."""
    from app.pic_checkin import _names
    from app.utils import today
    if d_to < d_from or (d_to - d_from).days >= MAX_DAYS:
        raise ScheduleError(f"Periode 1–{MAX_DAYS} hari")
    plan = db.fetch(f"""SELECT id, user_id::text, day, branch_code, note FROM {T} WHERE day BETWEEN %s AND %s ORDER BY day, branch_code""",
                    (d_from, d_to))
    visits = db.fetch(
        """SELECT user_id::text, (visited_at AT TIME ZONE 'Asia/Jakarta')::date AS day, branch_code, branch_name,
                  min(visited_at) AS first_in
             FROM integration_portal.pic_checkin WHERE kind = 'surveyor'
              AND (visited_at AT TIME ZONE 'Asia/Jakarta')::date BETWEEN %s AND %s GROUP BY 1, 2, 3, 4""", (d_from, d_to))
    seen = {(v["user_id"], v["day"], v["branch_code"]): v for v in visits}
    t = today()
    codes = sorted({r["branch_code"] for r in plan} | {v["branch_code"] for v in visits})
    names = _names(codes) if codes else {}
    rows = []
    planned_keys = set()
    for r in plan:
        k = (r["user_id"], r["day"], r["branch_code"])
        planned_keys.add(k)
        v = seen.get(k)
        rows.append({"id": r["id"], "userId": r["user_id"], "day": r["day"].isoformat(), "branchCode": r["branch_code"],
                     "branchName": names.get(r["branch_code"], r["branch_code"]), "note": r["note"],
                     "status": status_of(r["day"], v is not None, t), "clockIn": v["first_in"].isoformat() if v else None})
    unplanned = [{"userId": v["user_id"], "day": v["day"].isoformat(), "branchCode": v["branch_code"],
                  "branchName": v["branch_name"] or names.get(v["branch_code"], v["branch_code"]), "clockIn": v["first_in"].isoformat()}
                 for k, v in seen.items() if k not in planned_keys]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("done", "missed", "planned")}
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "team": team(), "data": rows,
            "unplanned": sorted(unplanned, key=lambda x: (x["day"], x["branchCode"])), "counts": {**counts, "unplanned": len(unplanned)}}


def set_day(user_id: str, day: date, branches: list[str], note: Optional[str], actor_id: str) -> dict:
    """The outlets of a member on a day (replaces that day's plan of the member; an empty list clears it)."""
    from app.utils import today
    if day < today() - timedelta(days=1):
        raise ScheduleError("Jadwal hari yang sudah lewat tidak diubah")
    if user_id not in {m["id"] for m in team()}:
        raise ScheduleError("Akun ini bukan anggota tim Cost Control (tidak membuka Cost Control Clock In)")
    codes = sorted({str(c).strip() for c in branches if str(c).strip()})
    if len(codes) > 20:
        raise ScheduleError("Paling banyak 20 outlet sehari")
    note = (note or "").strip()[:300] or None
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {T} WHERE user_id::text = %s AND day = %s", (user_id, day))
        for c in codes:
            conn.execute(f"""INSERT INTO {T} (user_id, day, branch_code, note, created_by) VALUES (%s, %s, %s, %s, %s)""",
                         (user_id, day, c, note, actor_id))
    return {"userId": user_id, "day": day.isoformat(), "branches": codes}


def copy_week(d_from: date, actor_id: str) -> dict:
    """The plan of the 7 days from `d_from` copied to the 7 days after (days already planned are kept)."""
    from app.utils import today
    target = d_from + timedelta(days=7)
    if target < today():
        raise ScheduleError("Minggu tujuan sudah lewat")
    with db.transaction() as conn:
        cur = conn.execute(
            f"""INSERT INTO {T} (user_id, day, branch_code, note, created_by)
                SELECT s.user_id, s.day + 7, s.branch_code, s.note, %s FROM {T} s
                 WHERE s.day BETWEEN %s AND %s AND s.day + 7 >= %s
                   AND NOT EXISTS (SELECT 1 FROM {T} x WHERE x.user_id = s.user_id AND x.day = s.day + 7)
                ON CONFLICT DO NOTHING""", (actor_id, d_from, d_from + timedelta(days=6), today()))
        n = cur.rowcount or 0
    return {"copied": n}


def mine(user_id: str, day: date) -> list[dict]:
    """The outlets planned for a member on a day (the Clock In form)."""
    from app.pic_checkin import _names
    rows = db.fetch(f"SELECT branch_code, note FROM {T} WHERE user_id::text = %s AND day = %s ORDER BY branch_code", (user_id, day))
    names = _names([r["branch_code"] for r in rows]) if rows else {}
    return [{"code": r["branch_code"], "name": names.get(r["branch_code"], r["branch_code"]), "note": r["note"]} for r in rows]
