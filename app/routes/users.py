"""User account management (superadmin; a COTR admin for the accounts of the COTR line, 2026-11-13).

    GET    /api/users?search=&role=&status=&line=&page=&pageSize=   line: kopicalf | wasgee | cotr (migration 054)
    POST   /api/users
    GET    /api/users/{id}
    PATCH  /api/users/{id}          (any subset of fields; "password" resets it)
    POST   /api/users/{id}/unlock
    DELETE /api/users/{id}
    PUT    /api/users/{id}/avatar   {"image": data URL}   DELETE /api/users/{id}/avatar

Safeguards: you cannot delete or deactivate yourself, and the last active
superadmin can be neither removed, deactivated nor demoted. A COTR admin (migration 099) sees and manages the accounts
of the COTR line only: roles user / outlet / driver / COTR admin, the line stays COTR, page exceptions of COTR pages.

`branches` (list of branch codes): the branches a "user" may see on the Overview and
Sales Transactions; required (at least one) for role "user", cleared for superadmins.
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import access, accounts, activity, avatars
from app.profile import ProfileError, clean_profile
from app.routes.auth import AvatarUpload, forget_sessions, require_account_admin
from app.security import EMAIL_RE, USERNAME_RE, hash_password, password_problem

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_account_admin)])


class UserFields(BaseModel):
    username: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=254)
    fullName: Optional[str] = Field(default=None, max_length=120)
    role: Optional[Literal["superadmin", "admin", "cost_control", "user", "outlet", "driver", "cotr_admin"]] = None
    isActive: Optional[bool] = None
    phoneNumber: Optional[str] = Field(default=None, max_length=32)
    jobTitle: Optional[str] = Field(default=None, max_length=80)
    department: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=500)
    mustChangePassword: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=128)
    employeeNumber: Optional[str] = Field(default=None, max_length=32)
    gender: Optional[Literal["male", "female", ""]] = None
    birthDate: Optional[str] = Field(default=None, max_length=10)
    address: Optional[str] = Field(default=None, max_length=300)
    city: Optional[str] = Field(default=None, max_length=80)
    workBranchCode: Optional[str] = Field(default=None, max_length=32)
    branches: Optional[list[str]] = Field(default=None, max_length=500)
    # pages added (true) or removed (false) on top of the role (app/access.py); None = leave as is
    pageOverrides: Optional[dict[str, bool]] = None
    businessLine: Optional[Literal["kopicalf", "wasgee", "cotr"]] = None
    # pages left out of the sidebar of the account (display only; migration 054)
    hiddenPages: Optional[list[str]] = Field(default=None, max_length=200)
    # Calf On The Road (2026-11-12): the employee of Master Driver the account belongs to ("" = none)
    cotrDriver: Optional[str] = Field(default=None, max_length=40)


def _error(message: str, status: int = 422, field: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"error": message, "field": field}, status_code=status)


def _cotr_admin(actor: dict) -> bool:
    return actor.get("role") == access.COTR_ADMIN


def _admin(actor: dict) -> bool:
    return actor.get("role") == access.ADMIN


def _out_of_scope(actor: dict, target: Optional[dict]) -> Optional[JSONResponse]:
    """A COTR admin touches the accounts of the COTR line only (never a superadmin or a Cost Control account); an admin
    (migration 105) the accounts of role User only, never its own."""
    if target and _cotr_admin(actor) and (target.get("business_line") != "cotr" or target["role"] not in access.COTR_ROLES):
        return _error("Admin COTR hanya dapat mengelola akun lini Calf On The Road", 403)
    if target and _admin(actor) and (target["role"] != "user" or str(target["id"]) == str(actor["id"])):
        return _error("Admin hanya dapat mengelola akun role User (akses akun Anda sendiri diatur superadmin)", 403)
    return None


def _admin_fields(actor: dict, body: "UserFields", data: dict) -> Optional[JSONResponse]:
    """What an admin may set: role User only, the pages it opens itself (it never gives more than it has)."""
    if not _admin(actor):
        return None
    if data.get("role", "user") != "user":
        return _error("Admin hanya dapat membuat akun role User", 403, "role")
    data["role"] = "user"
    if body.businessLine == "cotr":
        return _error("Admin tidak mengelola akun lini Calf On The Road", 403, "businessLine")
    if body.pageOverrides:
        mine = set(access.pages_for(actor))
        beyond = sorted(p for p, on in body.pageOverrides.items() if on and p not in mine)
        if beyond:
            return _error(f"Admin hanya dapat memberi halaman yang juga dimilikinya: {', '.join(beyond)}", 403, "pageOverrides")
    return None


def _cotr_fields(actor: dict, body: "UserFields", data: dict) -> Optional[JSONResponse]:
    """What a COTR admin may set: the COTR line, a COTR role, exceptions of COTR pages."""
    if not _cotr_admin(actor):
        return None
    if body.businessLine not in (None, "cotr"):
        return _error("Akun buatan admin COTR selalu lini Calf On The Road", 403, "businessLine")
    data["businessLine"] = "cotr"
    if "role" in data and data["role"] not in access.COTR_ROLES:
        return _error("Admin COTR hanya dapat memberi role User, Outlet, Driver atau Admin COTR", 403, "role")
    if body.pageOverrides and any(not access.cotr_page(p) for p in body.pageOverrides):
        return _error("Admin COTR hanya dapat mengatur halaman Calf On The Road", 403, "pageOverrides")
    return None


def _clean(body: UserFields, creating: bool, user_id: Optional[str] = None,
           current: Optional[dict] = None) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Normalised fields to store, or a validation error response."""
    data = body.model_dump(exclude_unset=True)
    data.pop("password", None)
    data.pop("cotrDriver", None)  # not a column of the account: tied in _after_save
    try:
        data.update(clean_profile(data))
    except ProfileError as exc:
        return None, _error(str(exc), field=exc.field)
    if "notes" in data:
        data["notes"] = (data["notes"] or "").strip() or None
    if "hiddenPages" in data:
        data["hiddenPages"] = access.clean_hidden(data["hiddenPages"])
    if "username" in data:
        data["username"] = (data["username"] or "").strip().lower()
        if not USERNAME_RE.match(data["username"]):
            return None, _error("Username 3-32 karakter: huruf kecil, angka, titik, garis bawah atau strip", field="username")
        if accounts.exists("username", data["username"], user_id):
            return None, _error("Username sudah dipakai", 409, "username")
    if "email" in data:
        data["email"] = (data["email"] or "").strip().lower()
        if not EMAIL_RE.match(data["email"]):
            return None, _error("Format email tidak valid", field="email")
        if accounts.exists("email", data["email"], user_id):
            return None, _error("Email sudah dipakai", 409, "email")
    if creating:
        # the login only; the user completes the profile themself under "My profile"
        for key, label in (("username", "Username"), ("email", "Email")):
            if not data.get(key):
                return None, _error(f"{label} wajib diisi", field=key)
        data.setdefault("role", "user")
        data.setdefault("isActive", True)
    # branches: required for role "user", none for superadmins
    if "branches" in data:
        codes = sorted({str(c).strip() for c in (data["branches"] or []) if str(c).strip()})
        unknown = set(codes) - accounts.known_branch_codes(codes)
        if unknown:
            return None, _error(f"Cabang tidak dikenal: {', '.join(sorted(unknown))}", field="branches")
        data["branches"] = codes
    role = data.get("role") or (current or {}).get("role") or "user"
    if role == access.COTR_ADMIN:
        data["businessLine"] = "cotr"  # a COTR admin is of the COTR line (migration 099)
    if role in accounts.GLOBAL_ROLES or role == "driver":  # superadmin / cost_control see every branch; drivers none (COTR)
        if creating or "branches" in data or (current or {}).get("branch_codes"):
            data["branches"] = []
    elif (data.get("businessLine") or (current or {}).get("business_line")) == "cotr":
        pass  # the COTR team works with pools, an ESB branch is optional
    else:
        branches = data["branches"] if "branches" in data else list((current or {}).get("branch_codes") or [])
        if not branches:
            return None, _error("Pilih minimal satu cabang untuk role User", field="branches")
    return data, None


def _password(body: UserFields, required: bool) -> tuple[Optional[str], Optional[JSONResponse]]:
    if not body.password:
        return None, (_error("Password wajib diisi", field="password") if required else None)
    problem = password_problem(body.password)
    if problem:
        return None, _error(problem, field="password")
    return hash_password(body.password), None


@router.get("")
def list_users(search: str = "", role: str = "", status: str = "", branch: str = "", line: str = "",
               page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100), actor: dict = Depends(require_account_admin)):
    if _cotr_admin(actor):
        line = "cotr"
    if _admin(actor):
        role = "user"  # an admin sees the accounts of role User only
    rows, total = accounts.list_users(search, role, status, pageSize, (page - 1) * pageSize, branch, line)
    return {"data": [_view(r) for r in rows], "total": total, "page": page, "pageSize": pageSize}


def _target(user: dict) -> dict:
    return {"targetId": str(user["id"]), "targetUsername": user["username"], "targetRole": user["role"]}


@router.get("/summary")
def users_summary(actor: dict = Depends(require_account_admin)):
    """Counts for the page header: totals, roles, status, users without branches, sign-in activity (a COTR admin: its line)."""
    c = accounts.user_counts("cotr" if _cotr_admin(actor) else None)
    return {"total": c.get("total", 0), "active": c.get("active", 0), "inactive": c.get("inactive", 0),
            "locked": c.get("locked", 0), "superadmins": c.get("superadmins", 0), "users": c.get("users", 0), "outlets": c.get("outlets", 0),
            "withoutBranch": c.get("without_branch", 0), "neverSignedIn": c.get("never_signed_in", 0),
            "active7d": c.get("active_7d", 0), "branchesCovered": c.get("branches_covered", 0),
            "lines": {k: c.get(f"line_{k}", 0) for k in accounts.LINES}}


@router.post("", status_code=201)
def create_user(body: UserFields, request: Request, actor: dict = Depends(require_account_admin)):
    data, error = _clean(body, creating=True)
    if error:
        return error
    error = _cotr_fields(actor, body, data) or _admin_fields(actor, body, data)
    if error:
        return error
    password_hash, error = _password(body, required=True)
    if error:
        return error
    user = accounts.create_user(data, password_hash, str(actor["id"]))
    error = _after_save(user, body, actor)
    if error:
        return error
    activity.record("user.create", user=actor, request=request, page="/users",
                    summary=f"Membuat akun {user['username']} ({user['role']})",
                    details={**_target(user), "email": user["email"], "branches": list(user.get("branch_codes") or [])})
    return JSONResponse({"user": _view(user)}, status_code=201)


@router.get("/{user_id}")
def get_user(user_id: UUID, actor: dict = Depends(require_account_admin)):
    user = accounts.get_user(str(user_id))
    if not user:
        return _error("User tidak ditemukan", 404)
    return _out_of_scope(actor, user) or {"user": _view(user)}


@router.patch("/{user_id}")
def update_user(user_id: UUID, body: UserFields, request: Request, actor: dict = Depends(require_account_admin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    error = _out_of_scope(actor, current)
    if error:
        return error
    data, error = _clean(body, creating=False, user_id=uid, current=current)
    if error:
        return error
    error = _cotr_fields(actor, body, data) or _admin_fields(actor, body, data)
    if error:
        return error
    password_hash, error = _password(body, required=False)
    if error:
        return error
    is_self = uid == str(actor["id"])
    if is_self and data.get("isActive") is False:
        return _error("Anda tidak dapat menonaktifkan akun Anda sendiri", 409, "isActive")
    leaves_superadmin = current["role"] == "superadmin" and current["is_active"] and (
        data.get("role", "superadmin") != "superadmin" or data.get("isActive", True) is False)
    if leaves_superadmin and accounts.count_active_superadmins(exclude_id=uid) == 0:
        return _error("Minimal harus ada satu superadmin aktif", 409, "role")
    user = accounts.update_user(uid, data, str(actor["id"]), password_hash)
    error = _after_save(user, body, actor)
    if error:
        return error
    if body.pageOverrides is not None and body.pageOverrides != access.user_overrides(uid):
        data["pageOverrides"] = body.pageOverrides
    changes = {k: {"from": _plain(current.get(accounts.EDITABLE.get(k, k))), "to": _plain(v)}
               for k, v in data.items() if k in accounts.EDITABLE and current.get(accounts.EDITABLE[k]) != v}
    if "branches" in data and sorted(current.get("branch_codes") or []) != data["branches"]:
        changes["branches"] = {"from": sorted(current.get("branch_codes") or []), "to": data["branches"]}
    activity.record("user.update", user=actor, request=request, page="/users",
                    summary=f"Mengubah akun {current['username']}" + (" (reset password)" if password_hash else ""),
                    details={**_target(current), "changes": changes, "passwordReset": bool(password_hash)})
    forget_sessions()
    return {"user": _view(user)}


def _view(row: dict) -> dict:
    """A user for this page: the account, the pages it opens and the exceptions to its role."""
    from app import cotr
    return {**accounts.public_user(row), "pages": access.pages_for(row), "cotrDriver": cotr.driver_of_account(str(row["id"])),
            "pageOverrides": ({p: v for p, v in access.user_overrides(str(row["id"])).items() if p in access.TEAM_PAGES and v}
                              if row["role"] == "superadmin" else access.user_overrides(str(row["id"])))}


def _after_save(user: dict, body: UserFields, actor: dict) -> Optional[JSONResponse]:
    """Page exceptions of the form, new job titles / departments into their lists, and the COTR employee of the account."""
    if body.cotrDriver is not None:
        from app import cotr
        try:
            cotr.link_account(str(user["id"]), body.cotrDriver.strip() or None)
        except cotr.CotrError as exc:
            return _error(str(exc), 422, "cotrDriver")
    for kind, value in (("jobTitle", user.get("job_title")), ("department", user.get("department"))):
        if value and value.strip():
            access.add_master(kind, value, str(actor["id"]))
    if body.pageOverrides is not None:
        try:
            access.set_user_overrides(str(user["id"]), access.clean_overrides(user["role"], body.pageOverrides),
                                      str(actor["id"]))
        except ValueError as exc:
            return _error(str(exc), 422, "pageOverrides")
    return None


def _plain(value):
    return value if value is None or isinstance(value, (str, int, float, bool, list)) else str(value)


@router.post("/{user_id}/unlock")
def unlock_user(user_id: UUID, request: Request, actor: dict = Depends(require_account_admin)):
    error = _out_of_scope(actor, accounts.get_user(str(user_id)))
    if error:
        return error
    user = accounts.unlock_user(str(user_id), str(actor["id"]))
    if user:
        activity.record("user.unlock", user=actor, request=request, page="/users",
                        summary=f"Membuka kunci akun {user['username']}", details=_target(user))
    return {"user": _view(user)} if user else _error("User tidak ditemukan", 404)


@router.put("/{user_id}/avatar")
def set_avatar(user_id: UUID, body: AvatarUpload, actor: dict = Depends(require_account_admin)):
    uid = str(user_id)
    target = accounts.get_user(uid)
    if not target:
        return _error("User tidak ditemukan", 404)
    error = _out_of_scope(actor, target)
    if error:
        return error
    parsed, problem = avatars.parse_data_url(body.image)
    if problem:
        return _error(problem, field="avatar")
    avatars.save(uid, *parsed, str(actor["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(uid))}


@router.delete("/{user_id}/avatar")
def delete_avatar(user_id: UUID, actor: dict = Depends(require_account_admin)):
    uid = str(user_id)
    target = accounts.get_user(uid)
    if not target:
        return _error("User tidak ditemukan", 404)
    error = _out_of_scope(actor, target)
    if error:
        return error
    avatars.remove(uid, str(actor["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(uid))}


@router.delete("/{user_id}")
def delete_user(user_id: UUID, request: Request, actor: dict = Depends(require_account_admin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    error = _out_of_scope(actor, current)
    if error:
        return error
    if uid == str(actor["id"]):
        return _error("Anda tidak dapat menghapus akun Anda sendiri", 409)
    if current["role"] == "superadmin" and current["is_active"] and accounts.count_active_superadmins(exclude_id=uid) == 0:
        return _error("Minimal harus ada satu superadmin aktif", 409)
    accounts.delete_user(uid)
    activity.record("user.delete", user=actor, request=request, page="/users",
                    summary=f"Menghapus akun {current['username']}", details={**_target(current), "email": current["email"]})
    forget_sessions()
    return {"ok": True}
