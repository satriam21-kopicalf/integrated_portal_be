"""GrabFood monitoring data (integration_esb.grab_*): outlet open/close status, official rating and customer reviews,
read from the Grab Merchant portal by scripts/sync_grab.js (integrated-esbapi) with a dashboard-refreshed session.

The portal only reads these tables and lets a super admin refresh the session cookie; it never holds the password.
"""
from typing import Optional

from app import database as db

SCHEMA = "integration_esb"


def _scope_sql(scope, alias="s") -> tuple[str, list]:
    if scope is None:
        return "", []
    if not scope:
        return f" AND {alias}.branch_code = '-'", []   # no branch: empty
    return f" AND {alias}.branch_code = ANY(%s)", [list(scope)]


def session_info() -> dict:
    """State of the saved merchant session for the dashboard (valid, when refreshed, when last used)."""
    row = db.fetchrow(f"SELECT is_valid, updated_at, updated_by, last_checked_at, note FROM {SCHEMA}.grab_session WHERE id = 1")
    if not row:
        return {"present": False, "valid": False}
    return {"present": True, "valid": row["is_valid"], "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
            "updatedBy": row["updated_by"], "lastCheckedAt": row["last_checked_at"].isoformat() if row["last_checked_at"] else None,
            "note": row["note"]}


def overview(scope) -> list[dict]:
    """One row per Grab outlet in scope: ESB branch, latest status, official rating, review summary (30 days)."""
    cond, params = _scope_sql(scope)
    rows = db.fetch(
        f"""
        SELECT s.merchant_id, s.merchant_name, s.branch_code, s.city_name,
               COALESCE(b.branch_name, s.merchant_name) AS branch_name,
               st.is_open, st.status_label, st.is_busy, st.close_reason, st.pause_cause, st.open_until,
               st.checked_at AS status_at,
               r.score, r.rating_count, r.distribution, r.captured_on AS rating_on,
               rv.review_count, rv.avg_recent, rv.low_count, rv.unreplied, rv.last_review_at
        FROM {SCHEMA}.grab_store s
        LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = s.branch_code
        LEFT JOIN {SCHEMA}.grab_store_status_now st ON st.merchant_id = s.merchant_id
        LEFT JOIN LATERAL (
            SELECT score, rating_count, distribution, captured_on FROM {SCHEMA}.grab_rating g
            WHERE g.merchant_id = s.merchant_id ORDER BY captured_on DESC LIMIT 1
        ) r ON true
        LEFT JOIN LATERAL (
            SELECT count(*)::int AS review_count, round(avg(rating), 2) AS avg_recent,
                   count(*) FILTER (WHERE rating <= 2)::int AS low_count,
                   count(*) FILTER (WHERE NOT has_reply)::int AS unreplied,
                   max(created_at) AS last_review_at
            FROM {SCHEMA}.grab_review gr WHERE gr.merchant_id = s.merchant_id AND gr.created_at >= NOW() - INTERVAL '30 days'
        ) rv ON true
        WHERE s.is_active {cond}
        ORDER BY branch_name
        """, params or None)
    out = []
    for r in rows:
        out.append({
            "merchantId": r["merchant_id"], "branchCode": r["branch_code"], "branchName": r["branch_name"],
            "cityName": r["city_name"],
            "status": {"isOpen": r["is_open"], "label": r["status_label"], "isBusy": r["is_busy"],
                       "closeReason": r["close_reason"], "pauseCause": r["pause_cause"],
                       "openUntil": r["open_until"].isoformat() if r["open_until"] else None,
                       "checkedAt": r["status_at"].isoformat() if r["status_at"] else None},
            "rating": {"score": float(r["score"]) if r["score"] is not None else None, "count": r["rating_count"],
                       "distribution": r["distribution"], "capturedOn": r["rating_on"].isoformat() if r["rating_on"] else None},
            "reviews30d": {"count": r["review_count"] or 0, "avg": float(r["avg_recent"]) if r["avg_recent"] is not None else None,
                           "low": r["low_count"] or 0, "unreplied": r["unreplied"] or 0,
                           "lastAt": r["last_review_at"].isoformat() if r["last_review_at"] else None},
        })
    return out


def reviews(branch: Optional[str], scope, limit: int = 100) -> list[dict]:
    """Recent reviews of one outlet (by ESB branch code), newest first."""
    cond, params = _scope_sql(scope)
    rows = db.fetch(
        f"""SELECT gr.created_at, gr.rating, gr.description, gr.ordered_items, gr.aspects, gr.has_reply, gr.service_type,
                   gr.image_urls, b.branch_name, s.merchant_name
            FROM {SCHEMA}.grab_review gr
            JOIN {SCHEMA}.grab_store s ON s.merchant_id = gr.merchant_id
            LEFT JOIN {SCHEMA}.master_branches b ON b.branch_code = s.branch_code
            WHERE s.branch_code = %s {cond} ORDER BY gr.created_at DESC LIMIT %s""",
        [branch, *params, limit])
    return [{
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None, "rating": r["rating"],
        "description": r["description"], "orderedItems": r["ordered_items"], "aspects": r["aspects"],
        "hasReply": r["has_reply"], "serviceType": r["service_type"], "imageUrls": r["image_urls"],
        "branchName": r["branch_name"], "merchantName": r["merchant_name"],
    } for r in rows]


def set_session(cookie: str, csrf: Optional[str], user: dict) -> dict:
    """Save a fresh merchant session (super admin pasted the cookie). csrf is derived from the cookie when absent."""
    import re
    cookie = (cookie or "").strip()
    if "mexusers_authn_token=" not in cookie:
        raise ValueError("Cookie Grab tidak valid: tidak ada mexusers_authn_token")
    if not csrf:
        m = re.search(r"mexusers_authn_token_jti=([^;]+)", cookie)
        csrf = m.group(1) if m else None
    with db.transaction() as c:
        c.execute(
            f"""INSERT INTO {SCHEMA}.grab_session (id, cookie, csrf, is_valid, updated_at, updated_by, note)
                VALUES (1, %s, %s, true, NOW(), %s, NULL)
                ON CONFLICT (id) DO UPDATE SET cookie = EXCLUDED.cookie, csrf = EXCLUDED.csrf, is_valid = true,
                  updated_at = NOW(), updated_by = EXCLUDED.updated_by, note = NULL""",
            (cookie, csrf, user.get("username")))
    return {"valid": True}
