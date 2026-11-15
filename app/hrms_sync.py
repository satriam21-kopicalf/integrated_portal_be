"""Job title and department of the person accounts from HRMS (2026-11-15, migration 110).

The accounts of role user / cost_control / admin are linked once to their HRMS employee (sk_hrms.employees): by the
saved link, else by name (an employee still working first), else by email, else by employee number; a name shared by
several employees, or no match, is linked by hand by a superadmin. Applying writes job title = positions.title and
department = org_units.name of the employee. Outlet / hub, driver and superadmin accounts are never touched, and
nothing here changes username, password, role, pages or branches: login is not affected.
"""
import re
import unicodedata
from typing import Optional

from app import access, activity
from app import database as db

P = "integration_portal"
SCOPE_ROLES = ("user", "cost_control", "admin")
GONE = "keluar"


class HrmsSyncError(ValueError):
    pass


def norm_name(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def available() -> bool:
    return bool(db.fetchrow("SELECT to_regclass('sk_hrms.employees') IS NOT NULL AS ok")["ok"])


def employees() -> list[dict]:
    return db.fetch("""SELECT e.id, e.employee_no, e.full_name, e.email, e.status,
                              p.title AS job_title, o.name AS department
                         FROM sk_hrms.employees e
                         LEFT JOIN sk_hrms.positions p ON p.id = e.position_id
                         LEFT JOIN sk_hrms.org_units o ON o.id = e.org_unit_id""")


def accounts() -> list[dict]:
    return db.fetch(f"""SELECT id::text, username, full_name, email, employee_number, role, is_active, job_title,
                               department, hrms_employee_id, hrms_linked_by, hrms_synced_at
                          FROM {P}.user_account WHERE role = ANY(%s) ORDER BY full_name NULLS LAST, username""",
                    (list(SCOPE_ROLES),))


def _index(emps: list[dict]) -> dict:
    idx = {"id": {}, "name": {}, "email": {}, "no": {}}
    for e in emps:
        idx["id"][e["id"]] = e
        idx["name"].setdefault(norm_name(e["full_name"]), []).append(e)
        if _norm(e["email"]):
            idx["email"].setdefault(_norm(e["email"]), []).append(e)
        if _norm(e["employee_no"]):
            idx["no"].setdefault(_norm(e["employee_no"]), []).append(e)
    return idx


def _one(hits: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """Exactly one employee; among several, the only one still working."""
    if len(hits) == 1:
        return hits[0], []
    working = [e for e in hits if _norm(e["status"]) != GONE]
    if len(working) == 1:
        return working[0], []
    return None, hits


def match(account: dict, idx: dict) -> dict:
    """{employee, by, candidates}: by = linked | name | email | employee_no | ambiguous | none."""
    linked = account.get("hrms_employee_id")
    if linked and linked in idx["id"]:
        return {"employee": idx["id"][linked], "by": "linked", "candidates": []}
    candidates: list[dict] = []
    for by, key, value in (("name", "name", norm_name(account.get("full_name"))),
                           ("email", "email", _norm(account.get("email"))),
                           ("employee_no", "no", _norm(account.get("employee_number")))):
        if not value:
            continue
        emp, many = _one(idx[key].get(value, []))
        if emp:
            return {"employee": emp, "by": by, "candidates": []}
        candidates = candidates or many
    return {"employee": None, "by": "ambiguous" if candidates else "none", "candidates": candidates}


def _emp_out(e: Optional[dict]) -> Optional[dict]:
    if not e:
        return None
    return {"id": e["id"], "employeeNo": e["employee_no"], "name": e["full_name"], "email": e["email"],
            "status": e["status"], "jobTitle": e["job_title"], "department": e["department"]}


def _row(a: dict, m: dict) -> dict:
    e = m["employee"]
    changes = bool(e) and ((e["job_title"] or "") != (a["job_title"] or "")
                           or (e["department"] or "") != (a["department"] or ""))
    return {"id": a["id"], "username": a["username"], "name": a["full_name"], "role": a["role"],
            "isActive": a["is_active"], "jobTitle": a["job_title"], "department": a["department"],
            "linkedBy": a["hrms_linked_by"], "syncedAt": a["hrms_synced_at"], "match": m["by"],
            "employee": _emp_out(e), "candidates": [_emp_out(c) for c in m["candidates"][:10]],
            "changes": changes, "employeeLeft": bool(e) and _norm(e["status"]) == GONE}


def preview() -> dict:
    if not available():
        raise HrmsSyncError("Data HRMS (sk_hrms) tidak tersedia di database ini.")
    idx = _index(employees())
    rows = [_row(a, match(a, idx)) for a in accounts()]
    summary: dict = {}
    for r in rows:
        summary[r["match"]] = summary.get(r["match"], 0) + 1
    summary["changes"] = sum(r["changes"] for r in rows)
    summary["employeeLeft"] = sum(r["employeeLeft"] for r in rows)
    return {"rows": rows, "summary": summary}


def apply(ids: Optional[list[str]], actor: dict, request=None) -> dict:
    """Link and write job title / department of the matched accounts (ids None = every matched account)."""
    if not available():
        raise HrmsSyncError("Data HRMS (sk_hrms) tidak tersedia di database ini.")
    idx = _index(employees())
    wanted = set(ids) if ids is not None else None
    done, skipped = [], 0
    with db.transaction() as conn:
        taken = {r["hrms_employee_id"]: r["id"] for r in conn.execute(
            f"SELECT id::text, hrms_employee_id FROM {P}.user_account WHERE hrms_employee_id IS NOT NULL")}
        for a in accounts():
            if wanted is not None and a["id"] not in wanted:
                continue
            m = match(a, idx)
            e = m["employee"]
            if not e or taken.get(e["id"], a["id"]) != a["id"]:
                skipped += 1
                continue
            conn.execute(f"""UPDATE {P}.user_account SET hrms_employee_id = %s,
                                    hrms_linked_by = COALESCE(CASE WHEN %s = 'linked' THEN hrms_linked_by END, %s),
                                    job_title = %s, department = %s, hrms_synced_at = now(), updated_at = now()
                              WHERE id = %s""",
                         (e["id"], m["by"], m["by"], e["job_title"], e["department"], a["id"]))
            taken[e["id"]] = a["id"]
            if (e["job_title"] or "") != (a["job_title"] or "") or (e["department"] or "") != (a["department"] or ""):
                done.append({"id": a["id"], "username": a["username"], "from": [a["job_title"], a["department"]],
                             "to": [e["job_title"], e["department"]]})
    for kind in ("jobTitle", "department"):
        for v in {d["to"][0 if kind == "jobTitle" else 1] for d in done}:
            if v and v.strip():
                access.add_master(kind, v, str(actor["id"]))
    activity.record("user.hrms_sync", user=actor, request=request,
                    summary=f"Sinkron HRMS: {len(done)} akun diperbarui, {skipped} dilewati",
                    details={"changed": done[:200], "skipped": skipped})
    return {"updated": len(done), "skipped": skipped}


def link(account_id: str, employee_id: Optional[str], actor: dict, request=None) -> dict:
    """Manual link (name shared by several employees, or no match); None removes the link."""
    acc = db.fetchrow(f"SELECT id::text, username, role FROM {P}.user_account WHERE id = %s", (account_id,))
    if not acc or acc["role"] not in SCOPE_ROLES:
        raise HrmsSyncError("Akun tidak ditemukan atau bukan akun orang (User / Cost Control / Admin).")
    if employee_id:
        if not db.fetchrow("SELECT 1 AS ok FROM sk_hrms.employees WHERE id = %s", (employee_id,)):
            raise HrmsSyncError("Karyawan HRMS tidak ditemukan.")
        other = db.fetchrow(f"SELECT username FROM {P}.user_account WHERE hrms_employee_id = %s AND id <> %s",
                            (employee_id, account_id))
        if other:
            raise HrmsSyncError(f"Karyawan ini sudah ditautkan ke akun {other['username']}.")
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.user_account SET hrms_employee_id = %s, hrms_linked_by = %s, updated_at = now()
                          WHERE id = %s""", (employee_id or None, "manual" if employee_id else None, account_id))
    activity.record("user.hrms_link", user=actor, request=request,
                    summary=f"Tautan HRMS {acc['username']}: {employee_id or 'dihapus'}")
    return apply([account_id], actor, request) if employee_id else {"updated": 0, "skipped": 0}


def search(q: str) -> list[dict]:
    like = f"%{q.strip()}%"
    rows = db.fetch("""SELECT e.id, e.employee_no, e.full_name, e.email, e.status, p.title AS job_title,
                              o.name AS department
                         FROM sk_hrms.employees e
                         LEFT JOIN sk_hrms.positions p ON p.id = e.position_id
                         LEFT JOIN sk_hrms.org_units o ON o.id = e.org_unit_id
                        WHERE e.full_name ILIKE %s OR e.email ILIKE %s OR e.employee_no ILIKE %s
                        ORDER BY (e.status = 'keluar'), e.full_name LIMIT 30""", (like, like, like))
    return [_emp_out(r) for r in rows]
