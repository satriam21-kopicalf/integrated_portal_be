"""Profile of the person accounts from HRMS (2026-11-15, migration 110): name, employee number, gender, birth date,
phone, address, work location (the ESB branch of the HRMS site), job title and department (SYNCED).

The accounts of role user / cost_control / admin, and of Calf On The Road driver / cotr_admin (same company in HRMS;
the employee id of Master Driver is their employee number), are linked once to their HRMS employee
(sk_hrms.employees): by the saved link, else by name (an employee still working first), else by email, else by employee
number; a name shared by several employees, or no match, is linked by hand by a superadmin. Applying writes the SYNCED
values (job title = positions.title, department = org_units.name); a value HRMS does not have keeps the portal one.
Outlet / hub and superadmin accounts are never touched. Email, username, password, role, pages and branches are never
synced: sign-in is not affected. Sensitive HRMS data (NIK, NPWP, BPJS, salary, bank) is not copied to the portal. The
department of Master Driver (cotr_driver, which COTR logic reads) is not changed either.
"""
import re
import unicodedata
from typing import Optional

from app import access, activity
from app import database as db
from app.profile import PROFILE_FIELDS

P = "integration_portal"
SCOPE_ROLES = ("user", "cost_control", "admin", "driver", "cotr_admin")
GONE = "keluar"
SYNCED = ("fullName", "employeeNumber", "gender", "birthDate", "phoneNumber", "address", "workBranchCode", "jobTitle",
          "department")
GENDER = {"L": "male", "P": "female"}


class HrmsSyncError(ValueError):
    pass


def norm_name(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def available() -> bool:
    return bool(db.fetchrow("SELECT to_regclass('sk_hrms.employees') IS NOT NULL AS ok")["ok"])


def employees(where: str = "", params: tuple = ()) -> list[dict]:
    return db.fetch(f"""SELECT e.id, e.employee_no, e.full_name, e.email, e.status, e.gender, e.birth_date, e.phone,
                               e.address, e.join_date, s.name AS site_name, b.branch_code AS work_branch_code,
                               p.title AS job_title, o.name AS department
                          FROM sk_hrms.employees e
                          LEFT JOIN sk_hrms.positions p ON p.id = e.position_id
                          LEFT JOIN sk_hrms.org_units o ON o.id = e.org_unit_id
                          LEFT JOIN sk_hrms.sites s ON s.id = e.site_id
                          LEFT JOIN LATERAL (SELECT mb.branch_code FROM integration_esb.master_branches mb
                                              WHERE mb.branch_code = s.esb_branch_code LIMIT 1) b ON true
                          {where}""", params)


def _txt(v) -> Optional[str]:
    return (str(v).strip() or None) if v is not None else None


def values(e: dict) -> dict:
    """The portal profile values of an HRMS employee (keys of SYNCED; None = HRMS has none: keep the portal one)."""
    return {"fullName": _txt(e.get("full_name")), "employeeNumber": _txt(e.get("employee_no")),
            "gender": GENDER.get((e.get("gender") or "").strip().upper()), "birthDate": e.get("birth_date"),
            "phoneNumber": _txt(e.get("phone")), "address": _txt(e.get("address")),
            "workBranchCode": e.get("work_branch_code"), "jobTitle": _txt(e.get("job_title")),
            "department": _txt(e.get("department"))}


def diff(a: dict, e: dict) -> list[str]:
    """The SYNCED keys whose HRMS value differs from the account's (a: user_account row)."""
    return [k for k, v in values(e).items() if v is not None and str(v) != str(a.get(PROFILE_FIELDS[k]) or "")]


def accounts() -> list[dict]:
    return db.fetch(f"""SELECT u.id::text, u.username, u.full_name, u.email, u.role, u.is_active, u.job_title,
                               u.department, u.gender, u.birth_date, u.phone_number, u.address, u.work_branch_code,
                               u.business_line, u.hrms_employee_id, u.hrms_linked_by, u.hrms_synced_at,
                               u.employee_number AS own_employee_number,
                               COALESCE(NULLIF(u.employee_number, ''),
                                        (SELECT d.employee_id FROM {P}.cotr_driver d WHERE d.user_id = u.id LIMIT 1))
                                   AS employee_number
                          FROM {P}.user_account u WHERE u.role = ANY(%s) AND u.account_kind = 'person'
                         ORDER BY u.full_name NULLS LAST, u.username""",
                    (list(SCOPE_ROLES),))


def _own(a: dict) -> dict:
    """The account as stored (the employee number of Master Driver only helps the match)."""
    return {**a, "employee_number": a.get("own_employee_number", a.get("employee_number"))}


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


def _iso(v) -> Optional[str]:
    return v.isoformat() if v else None


def _emp_out(e: Optional[dict]) -> Optional[dict]:
    if not e:
        return None
    return {"id": e["id"], "employeeNo": e["employee_no"], "name": e["full_name"], "email": e["email"],
            "status": e["status"], "jobTitle": e["job_title"], "department": e["department"],
            "gender": GENDER.get((e.get("gender") or "").strip().upper()), "birthDate": _iso(e.get("birth_date")),
            "phone": e.get("phone"), "address": e.get("address"), "workBranchCode": e.get("work_branch_code"),
            "site": e.get("site_name"), "joinDate": _iso(e.get("join_date"))}


def _row(a: dict, m: dict) -> dict:
    e = m["employee"]
    fields = diff(_own(a), e) if e else []
    return {"id": a["id"], "username": a["username"], "name": a["full_name"], "role": a["role"],
            "businessLine": a.get("business_line"), "isActive": a["is_active"], "jobTitle": a["job_title"],
            "department": a["department"], "linkedBy": a["hrms_linked_by"], "syncedAt": a["hrms_synced_at"],
            "match": m["by"], "employee": _emp_out(e), "candidates": [_emp_out(c) for c in m["candidates"][:10]],
            "changes": bool(fields), "changedFields": fields,
            "employeeLeft": bool(e) and _norm(e["status"]) == GONE}


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
    """Link and write the HRMS profile values (SYNCED) of the matched accounts (ids None = every matched account)."""
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
            vals = {k: v for k, v in values(e).items() if v is not None}
            fields = diff(_own(a), e)
            sets = "".join(f", {PROFILE_FIELDS[k]} = %({k})s" for k in vals)
            conn.execute(f"""UPDATE {P}.user_account SET hrms_employee_id = %(emp)s, hrms_status = %(st)s,
                                    hrms_linked_by = COALESCE(CASE WHEN %(by)s = 'linked' THEN hrms_linked_by END,
                                                              %(by)s),
                                    hrms_synced_at = now(), updated_at = now(){sets}
                              WHERE id = %(id)s""", {**vals, "emp": e["id"], "st": e["status"], "by": m["by"], "id": a["id"]})
            taken[e["id"]] = a["id"]
            if fields:
                done.append({"id": a["id"], "username": a["username"], "fields": fields,
                             "to": [vals.get("jobTitle"), vals.get("department")]})
    for i, kind in enumerate(("jobTitle", "department")):
        for v in {d["to"][i] for d in done}:
            if v and v.strip():
                access.add_master(kind, v, str(actor["id"]))
    if done:
        from app.routes.auth import forget_sessions
        forget_sessions()
    activity.record("user.hrms_sync", user=actor, request=request,
                    summary=f"Sinkron HRMS: {len(done)} akun diperbarui, {skipped} dilewati",
                    details={"changed": done[:200], "skipped": skipped})
    return {"updated": len(done), "skipped": skipped}


def link(account_id: str, employee_id: Optional[str], actor: dict, request=None) -> dict:
    """Manual link (name shared by several employees, or no match); None removes the link."""
    acc = db.fetchrow(f"SELECT id::text, username, role FROM {P}.user_account WHERE id = %s", (account_id,))
    if not acc or acc["role"] not in SCOPE_ROLES:
        raise HrmsSyncError("Akun tidak ditemukan atau bukan akun orang (User / Inventory Surveyor / Admin / Driver COTR).")
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


def locked(row: dict) -> list[str]:
    """The SYNCED keys a linked account cannot type: those holding a value (HRMS had one); an empty one stays
    editable, so a profile is never stuck incomplete."""
    if not row.get("hrms_employee_id"):
        return []
    return [k for k in SYNCED if row.get(PROFILE_FIELDS[k]) not in (None, "")]


def fill(data: dict, current: Optional[dict], role: str) -> Optional[str]:
    """User Accounts create / edit: an HRMS employee given fills the SYNCED values from HRMS; a linked account keeps
    them (typed ones are ignored); a role outside the scope (outlet / hub, superadmin) is unlinked. Returns an error
    message or None."""
    if role not in SCOPE_ROLES:
        if data.get("hrmsEmployeeId"):
            return "Hanya akun orang (User / Inventory Surveyor / Admin / Driver COTR) yang ditautkan ke HRMS."
        if (current or {}).get("hrms_employee_id") or "hrmsEmployeeId" in data:
            data.update(hrmsEmployeeId=None, hrmsLinkedBy=None)
        return None
    # a person account is made from its HRMS employee; a shared / test account is never linked (migration 115)
    kind = data.get("accountKind") or (current or {}).get("account_kind") or "person"
    if kind != "person":
        if data.get("hrmsEmployeeId"):
            return "Akun bersama / testing tidak ditautkan ke karyawan HRMS."
        if (current or {}).get("hrms_employee_id") or "hrmsEmployeeId" in data:
            data.update(hrmsEmployeeId=None, hrmsLinkedBy=None)
        return None
    need = "Pilih karyawan HRMS untuk akun orang (atau tandai sebagai akun bersama / testing)."
    if "hrmsEmployeeId" not in data:
        # only a new account, one turned into a person role or into a person: editing, deactivating or resetting
        # the password of an existing account is never blocked
        cur = current or {}
        becomes = not cur or cur.get("role") not in SCOPE_ROLES or (cur.get("account_kind") or "person") != "person"
        if not cur.get("hrms_employee_id") and becomes:
            return need
        for k in locked(current or {}):
            data.pop(k, None)
        return None
    emp_id = (data["hrmsEmployeeId"] or "").strip() or None
    data["hrmsEmployeeId"] = emp_id
    if not emp_id:
        return need
    if not available():
        return "Data HRMS (sk_hrms) tidak tersedia di database ini."
    found = employees("WHERE e.id = %s", (emp_id,))
    if not found:
        return "Karyawan HRMS tidak ditemukan."
    other = db.fetchrow(f"SELECT username FROM {P}.user_account WHERE hrms_employee_id = %s AND id::text <> %s",
                        (emp_id, str((current or {}).get("id") or "")))
    if other:
        return f"Karyawan ini sudah ditautkan ke akun {other['username']}."
    data.update({k: v for k, v in values(found[0]).items() if v is not None}, hrmsLinkedBy="manual")
    return None


def search(q: str) -> list[dict]:
    like = f"%{q.strip()}%"
    rows = employees("WHERE e.full_name ILIKE %s OR e.email ILIKE %s OR e.employee_no ILIKE %s "
                     "ORDER BY (e.status = 'keluar'), e.full_name LIMIT 30", (like, like, like))
    return [_emp_out(r) for r in rows]


def employee(employee_id: str) -> Optional[dict]:
    """One HRMS employee as the User Accounts detail shows it (status, site, join date …)."""
    rows = employees("WHERE e.id = %s", (employee_id,))
    return _emp_out(rows[0]) if rows else None


def sync_linked() -> dict:
    """Hourly (scripts/hrms-sync.cron): the SYNCED values and the HRMS status of every account already linked to an
    employee. Never links new accounts (a match by name is confirmed by a superadmin) and never deactivates one: an
    employee who left HRMS is only marked (hrms_status = keluar)."""
    if not available():
        return {"skipped": "sk_hrms not available"}
    linked = [a for a in accounts() if a["hrms_employee_id"]]
    emps = {e["id"]: e for e in employees("WHERE e.id = ANY(%s)", ([a["hrms_employee_id"] for a in linked],))}         if linked else {}
    status = {r["id"]: r["hrms_status"] for r in db.fetch(
        f"SELECT id::text, hrms_status FROM {P}.user_account WHERE hrms_employee_id IS NOT NULL")}
    changed, left, missing = [], [], 0
    with db.transaction() as conn:
        for a in linked:
            e = emps.get(a["hrms_employee_id"])
            if not e:
                missing += 1
                continue
            fields = diff(_own(a), e)
            if status.get(a["id"]) != e["status"]:
                fields.append("hrmsStatus")
                if _norm(e["status"]) == GONE:
                    left.append(a["username"])
            if not fields:
                continue
            vals = {k: v for k, v in values(e).items() if v is not None}
            sets = "".join(f", {PROFILE_FIELDS[k]} = %({k})s" for k in vals)
            conn.execute(f"""UPDATE {P}.user_account SET hrms_status = %(st)s, hrms_synced_at = now(),
                                    updated_at = now(){sets} WHERE id = %(id)s""",
                         {**vals, "st": e["status"], "id": a["id"]})
            changed.append({"username": a["username"], "fields": fields})
    for kind in ("jobTitle", "department"):
        for v in {values(emps[a["hrms_employee_id"]])[kind] for a in linked if a["hrms_employee_id"] in emps}:
            if v:
                access.add_master(kind, v, None)
    if changed:
        from app.routes.auth import forget_sessions
        forget_sessions()
        activity.record("user.hrms_sync", username="system",
                        summary=f"Sinkron HRMS otomatis: {len(changed)} akun diperbarui"
                                + (f", {len(left)} karyawan keluar ditandai" if left else ""),
                        details={"changed": changed[:200], "left": left, "missing": missing})
    return {"linked": len(linked), "updated": len(changed), "left": left, "missing": missing}


if __name__ == "__main__":
    import json
    from datetime import datetime, timezone

    db.open_pool(wait=True)
    try:
        print(datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(sync_linked(), default=str))
    finally:
        db.close_pool()
