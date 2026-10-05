"""User accounts and login sessions (schema integration_portal).

All SQL for user_account / user_session lives here, so the routes stay thin
and tests can swap this module's functions for an in-memory store.

CLI (first superadmin, or emergency password reset):
    python -m app.accounts create --username superadmin --email admin@kopicalf.co.id --full-name "Super Admin" --role superadmin
    python -m app.accounts reset-password --username superadmin
The password is generated and printed once (or taken from PORTAL_NEW_PASSWORD).
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app import database as db
from app.profile import PROFILE_FIELDS, is_complete
from app.security import generate_password, hash_password, password_problem

T_USER = "integration_portal.user_account"
T_SESSION = "integration_portal.user_session"
T_USER_BRANCH = "integration_portal.user_branch"

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
ROLES = ("superadmin", "user")

USER_COLUMNS = """u.id, u.username, u.email, u.full_name, u.role, u.is_active, u.phone_number, u.job_title,
    u.department, u.notes, u.must_change_password, u.last_login_at, u.last_login_ip,
    u.failed_login_attempts, u.locked_until, u.password_changed_at, u.created_at, u.updated_at, u.avatar_updated_at,
    u.employee_number, u.gender, u.birth_date, u.address, u.city, u.work_branch_code, u.profile_updated_at,
    (SELECT b.branch_name FROM integration_esb.master_branches b WHERE b.branch_code = u.work_branch_code
     ORDER BY COALESCE(b.is_deleted, false) LIMIT 1) AS work_branch_name,
    (SELECT array_agg(x.branch_code ORDER BY x.branch_code) FROM integration_portal.user_branch x
     WHERE x.user_id = u.id) AS branch_codes,
    cb.username AS created_by_username, ub.username AS updated_by_username"""
USER_FROM = f"""{T_USER} u
    LEFT JOIN {T_USER} cb ON cb.id = u.created_by
    LEFT JOIN {T_USER} ub ON ub.id = u.updated_by"""

# API field name -> column (fields an admin may set); users edit only PROFILE_FIELDS themselves
EDITABLE = {
    "username": "username", "email": "email", "role": "role", "isActive": "is_active", "notes": "notes",
    "mustChangePassword": "must_change_password", **PROFILE_FIELDS,
}


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def public_user(row: Optional[dict]) -> Optional[dict]:
    """API shape of a user (never includes the password hash)."""
    if not row:
        return None
    locked = row.get("locked_until")
    avatar = row.get("avatar_updated_at")
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "email": row["email"],
        "fullName": row["full_name"],
        # what to show: the full name, or the username until the user completes the profile
        "displayName": row["full_name"] or row["username"],
        "role": row["role"],
        "isActive": row["is_active"],
        "isLocked": bool(locked and locked > datetime.now(timezone.utc)),
        "phoneNumber": row.get("phone_number"),
        "jobTitle": row.get("job_title"),
        "department": row.get("department"),
        "notes": row.get("notes"),
        "employeeNumber": row.get("employee_number"),
        "gender": row.get("gender"),
        "birthDate": _iso(row.get("birth_date")),
        "address": row.get("address"),
        "city": row.get("city"),
        "workBranchCode": row.get("work_branch_code"),
        "workBranchName": row.get("work_branch_name"),
        # branches a "user" may see (superadmins see every branch)
        "branches": list(row.get("branch_codes") or []),
        "profileComplete": is_complete(row),
        "profileUpdatedAt": _iso(row.get("profile_updated_at")),
        "mustChangePassword": row.get("must_change_password", False),
        # versioned, so the browser may cache the image indefinitely
        "avatarUrl": f"/api/avatars/{row['id']}?v={int(avatar.timestamp())}" if avatar else None,
        "lastLoginAt": _iso(row.get("last_login_at")),
        "lastLoginIp": row.get("last_login_ip"),
        "failedLoginAttempts": row.get("failed_login_attempts", 0),
        "lockedUntil": _iso(locked),
        "passwordChangedAt": _iso(row.get("password_changed_at")),
        "createdAt": _iso(row.get("created_at")),
        "createdBy": row.get("created_by_username"),
        "updatedAt": _iso(row.get("updated_at")),
        "updatedBy": row.get("updated_by_username"),
    }


# ---------------------------------------------------------------- reads

def find_for_login(identifier: str, method: str) -> Optional[dict]:
    column = "email" if method == "email" else "username"
    return db.fetchrow(
        f"SELECT u.*, NULL AS created_by_username, NULL AS updated_by_username FROM {T_USER} u "
        f"WHERE lower(u.{column}) = lower(%s)", (identifier.strip(),))


def get_user(user_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT {USER_COLUMNS} FROM {USER_FROM} WHERE u.id = %s", (user_id,))


def get_password_hash(user_id: str) -> Optional[str]:
    row = db.fetchrow(f"SELECT password_hash FROM {T_USER} WHERE id = %s", (user_id,))
    return row["password_hash"] if row else None


def list_users(search: str = "", role: str = "", status: str = "", limit: int = 20, offset: int = 0,
               branch: str = "") -> tuple[list[dict], int]:
    where, params = ["TRUE"], {}
    if search:
        where.append("(u.username ILIKE %(q)s OR u.email ILIKE %(q)s OR u.full_name ILIKE %(q)s "
                     "OR u.employee_number ILIKE %(q)s OR u.phone_number ILIKE %(q)s OR u.job_title ILIKE %(q)s)")
        params["q"] = f"%{search.strip()}%"
    if branch:
        # users assigned to the branch (superadmins see every branch, so they are listed too)
        where.append(f"(u.role = 'superadmin' OR EXISTS (SELECT 1 FROM {T_USER_BRANCH} x "
                     "WHERE x.user_id = u.id AND x.branch_code = %(branch)s))")
        params["branch"] = branch.strip()
    if role in ROLES:
        where.append("u.role = %(role)s")
        params["role"] = role
    if status == "active":
        where.append("u.is_active")
    elif status == "inactive":
        where.append("NOT u.is_active")
    elif status == "locked":
        where.append("u.locked_until > now()")
    cond = " AND ".join(where)
    total = db.fetchrow(f"SELECT count(*)::int AS n FROM {T_USER} u WHERE {cond}", params)["n"]
    rows = db.fetch(
        f"SELECT {USER_COLUMNS} FROM {USER_FROM} WHERE {cond} ORDER BY COALESCE(u.full_name, u.username), u.username "
        "LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": limit, "offset": offset},
    )
    return rows, total


def user_counts() -> dict:
    """Headline counts for the User Accounts page."""
    return db.fetchrow(f"""
        SELECT count(*)::int AS total,
               count(*) FILTER (WHERE u.is_active)::int AS active,
               count(*) FILTER (WHERE NOT u.is_active)::int AS inactive,
               count(*) FILTER (WHERE u.locked_until > now())::int AS locked,
               count(*) FILTER (WHERE u.role = 'superadmin')::int AS superadmins,
               count(*) FILTER (WHERE u.role = 'user')::int AS users,
               count(*) FILTER (WHERE u.role = 'user' AND NOT EXISTS (
                   SELECT 1 FROM {T_USER_BRANCH} x WHERE x.user_id = u.id))::int AS without_branch,
               count(*) FILTER (WHERE u.last_login_at IS NULL)::int AS never_signed_in,
               count(*) FILTER (WHERE u.last_login_at > now() - interval '7 days')::int AS active_7d,
               (SELECT count(DISTINCT x.branch_code)::int FROM {T_USER_BRANCH} x
                JOIN {T_USER} xu ON xu.id = x.user_id AND xu.role = 'user' AND xu.is_active) AS branches_covered
        FROM {T_USER} u
    """) or {}


def known_branch_codes(codes: list[str]) -> set[str]:
    """The codes that exist in the ESB branch master."""
    if not codes:
        return set()
    rows = db.fetch("SELECT DISTINCT branch_code FROM integration_esb.master_branches WHERE branch_code = ANY(%s)", (codes,))
    return {r["branch_code"] for r in rows}


def _set_branches(conn, user_id: str, codes: list[str], actor_id: Optional[str]) -> None:
    conn.execute(f"DELETE FROM {T_USER_BRANCH} WHERE user_id = %s", (user_id,))
    for code in codes:
        conn.execute(f"INSERT INTO {T_USER_BRANCH} (user_id, branch_code, created_by) VALUES (%s, %s, %s)",
                     (user_id, code, actor_id))


def exists(column: str, value: str, exclude_id: Optional[str] = None) -> bool:
    assert column in ("username", "email")
    row = db.fetchrow(
        f"SELECT 1 AS x FROM {T_USER} WHERE lower({column}) = lower(%s) AND (%s::uuid IS NULL OR id <> %s::uuid)",
        (value, exclude_id, exclude_id))
    return row is not None


def count_active_superadmins(exclude_id: Optional[str] = None) -> int:
    row = db.fetchrow(
        f"SELECT count(*)::int AS n FROM {T_USER} WHERE role = 'superadmin' AND is_active "
        "AND (%s::uuid IS NULL OR id <> %s::uuid)", (exclude_id, exclude_id))
    return row["n"] if row else 0


# ---------------------------------------------------------------- writes

def create_user(data: dict, password_hash: str, actor_id: Optional[str]) -> dict:
    cols = {EDITABLE[k]: v for k, v in data.items() if k in EDITABLE}
    cols.update(password_hash=password_hash, created_by=actor_id, updated_by=actor_id)
    names = ", ".join(cols)
    values = ", ".join(f"%({c})s" for c in cols)
    with db.transaction() as conn:
        row = conn.execute(f"INSERT INTO {T_USER} ({names}) VALUES ({values}) RETURNING id", cols).fetchone()
        if data.get("branches"):
            _set_branches(conn, str(row["id"]), data["branches"], actor_id)
    return get_user(str(row["id"]))


def update_user(user_id: str, data: dict, actor_id: Optional[str], password_hash: Optional[str] = None) -> Optional[dict]:
    cols = {EDITABLE[k]: v for k, v in data.items() if k in EDITABLE}
    if password_hash:
        cols.update(password_hash=password_hash, failed_login_attempts=0, locked_until=None)
    sets = [f"{c} = %({c})s" for c in cols] + ["updated_at = now()", "updated_by = %(actor)s"]
    if password_hash:
        sets.append("password_changed_at = now()")
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_USER} SET {', '.join(sets)} WHERE id = %(id)s", {**cols, "actor": actor_id, "id": user_id})
        if "branches" in data:
            _set_branches(conn, user_id, data["branches"], actor_id)
        # a new password, deactivation or role change signs the user out everywhere
        if password_hash or data.get("isActive") is False or "role" in data:
            conn.execute(f"UPDATE {T_SESSION} SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    return get_user(user_id)


def update_profile(user_id: str, data: dict) -> Optional[dict]:
    """The user's own changes to their profile ("My profile")."""
    cols = {PROFILE_FIELDS[k]: v for k, v in data.items() if k in PROFILE_FIELDS}
    if cols:
        sets = [f"{c} = %({c})s" for c in cols] + ["profile_updated_at = now()", "updated_at = now()", "updated_by = %(id)s"]
        with db.transaction() as conn:
            conn.execute(f"UPDATE {T_USER} SET {', '.join(sets)} WHERE id = %(id)s", {**cols, "id": user_id})
    return get_user(user_id)


def unlock_user(user_id: str, actor_id: Optional[str]) -> Optional[dict]:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_USER} SET failed_login_attempts = 0, locked_until = NULL, updated_at = now(), "
                     "updated_by = %s WHERE id = %s", (actor_id, user_id))
    return get_user(user_id)


def delete_user(user_id: str) -> bool:
    with db.transaction() as conn:
        cur = conn.execute(f"DELETE FROM {T_USER} WHERE id = %s", (user_id,))
        return cur.rowcount > 0


def record_login_failure(user_id: str) -> dict:
    """Counts a wrong password; locks the account after MAX_FAILED_LOGINS in a row."""
    with db.transaction() as conn:
        return conn.execute(
            f"""UPDATE {T_USER} SET failed_login_attempts = failed_login_attempts + 1,
                    locked_until = CASE WHEN failed_login_attempts + 1 >= %(max)s
                                        THEN now() + make_interval(mins => %(mins)s) ELSE locked_until END
                WHERE id = %(id)s RETURNING failed_login_attempts, locked_until""",
            {"id": user_id, "max": MAX_FAILED_LOGINS, "mins": LOCK_MINUTES},
        ).fetchone()


def record_login_success(user_id: str, ip: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_USER} SET last_login_at = now(), last_login_ip = %s, failed_login_attempts = 0, "
                     "locked_until = NULL WHERE id = %s", (ip, user_id))


# ---------------------------------------------------------------- sessions

def create_session(user_id: str, token_hash: str, ttl: timedelta, ip: Optional[str], user_agent: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(
            f"INSERT INTO {T_SESSION} (user_id, token_hash, expires_at, ip_address, user_agent) "
            "VALUES (%s, %s, now() + %s, %s, %s)", (user_id, token_hash, ttl, ip, (user_agent or "")[:300]))
        # housekeeping: forget sessions that ended more than 30 days ago
        conn.execute(f"DELETE FROM {T_SESSION} WHERE expires_at < now() - interval '30 days'")


def session_user(token_hash: str) -> Optional[dict]:
    """The active user behind a session token hash (None if revoked, expired or inactive)."""
    row = db.fetchrow(
        f"""SELECT {USER_COLUMNS}, s.id AS session_id, s.last_seen_at
            FROM {T_SESSION} s JOIN {USER_FROM} ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() AND u.is_active""",
        (token_hash,))
    if row and (row["last_seen_at"] is None or datetime.now(timezone.utc) - row["last_seen_at"] > timedelta(minutes=5)):
        with db.transaction() as conn:
            conn.execute(f"UPDATE {T_SESSION} SET last_seen_at = now() WHERE id = %s", (row["session_id"],))
    return row


def revoke_session(token_hash: str) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_SESSION} SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL", (token_hash,))


def revoke_other_sessions(user_id: str, keep_token_hash: str) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_SESSION} SET revoked_at = now() WHERE user_id = %s AND token_hash <> %s "
                     "AND revoked_at IS NULL", (user_id, keep_token_hash))


def set_own_password(user_id: str, password_hash: str) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE {T_USER} SET password_hash = %s, password_changed_at = now(), must_change_password = false, "
                     "updated_at = now(), updated_by = id WHERE id = %s", (password_hash, user_id))


# ---------------------------------------------------------------- CLI

def _cli_password() -> str:
    pw = os.environ.get("PORTAL_NEW_PASSWORD") or generate_password()
    problem = password_problem(pw)
    if problem:
        sys.exit(problem)
    return pw


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Manage dashboard user accounts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create an account (password is generated and printed once)")
    c.add_argument("--username", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--full-name", default="", help="optional: the user can fill it in under My profile")
    c.add_argument("--role", choices=ROLES, default="user")
    c.add_argument("--branches", default="", help="branch codes a 'user' may see, comma separated")
    r = sub.add_parser("reset-password", help="set a new generated password and unlock the account")
    r.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    db.open_pool(wait=True)
    try:
        password = _cli_password()
        if args.cmd == "create":
            if exists("username", args.username) or exists("email", args.email):
                sys.exit("username or email already exists")
            branches = sorted({b.strip() for b in args.branches.split(",") if b.strip()})
            unknown = set(branches) - known_branch_codes(branches)
            if unknown:
                sys.exit(f"unknown branch code(s): {', '.join(sorted(unknown))}")
            if args.role == "user" and not branches:
                sys.exit("a 'user' needs --branches")
            user = create_user({"username": args.username.lower(), "email": args.email.lower(), "fullName": args.full_name or None,
                                "role": args.role, "isActive": True, "mustChangePassword": True,
                                "branches": branches if args.role == "user" else []},
                               hash_password(password), None)
        else:
            row = find_for_login(args.username, "username")
            if not row:
                sys.exit("no such user")
            user = update_user(str(row["id"]), {"mustChangePassword": True}, None, hash_password(password))
        print(f"{args.cmd}: {user['username']} <{user['email']}> role={user['role']}")
        print(f"password (change it after signing in): {password}")
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
