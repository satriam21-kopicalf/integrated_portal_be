"""User account management (superadmin only).

    GET    /api/users?search=&role=&status=&page=&pageSize=
    POST   /api/users
    GET    /api/users/{id}
    PATCH  /api/users/{id}          (any subset of fields; "password" resets it)
    POST   /api/users/{id}/unlock
    DELETE /api/users/{id}
    PUT    /api/users/{id}/avatar   {"image": data URL}   DELETE /api/users/{id}/avatar

Safeguards: you cannot delete or deactivate yourself, and the last active
superadmin can be neither removed, deactivated nor demoted.

`branches` (list of branch codes): the branches a "user" may see on the Overview and
Sales Transactions; required (at least one) for role "user", cleared for superadmins.
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import accounts, activity, avatars
from app.profile import ProfileError, clean_profile
from app.routes.auth import AvatarUpload, forget_sessions, require_superadmin
from app.security import EMAIL_RE, USERNAME_RE, hash_password, password_problem

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_superadmin)])


class UserFields(BaseModel):
    username: Optional[str] = Field(default=None, max_length=32)
    email: Optional[str] = Field(default=None, max_length=254)
    fullName: Optional[str] = Field(default=None, max_length=120)
    role: Optional[Literal["superadmin", "user"]] = None
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


def _error(message: str, status: int = 422, field: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"error": message, "field": field}, status_code=status)


def _clean(body: UserFields, creating: bool, user_id: Optional[str] = None,
           current: Optional[dict] = None) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Normalised fields to store, or a validation error response."""
    data = body.model_dump(exclude_unset=True)
    data.pop("password", None)
    try:
        data.update(clean_profile(data))
    except ProfileError as exc:
        return None, _error(str(exc), field=exc.field)
    if "notes" in data:
        data["notes"] = (data["notes"] or "").strip() or None
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
    if role == "superadmin":
        if creating or "branches" in data or (current or {}).get("branch_codes"):
            data["branches"] = []
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
def list_users(search: str = "", role: str = "", status: str = "", branch: str = "",
               page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
    rows, total = accounts.list_users(search, role, status, pageSize, (page - 1) * pageSize, branch)
    return {"data": [accounts.public_user(r) for r in rows], "total": total, "page": page, "pageSize": pageSize}


def _target(user: dict) -> dict:
    return {"targetId": str(user["id"]), "targetUsername": user["username"], "targetRole": user["role"]}


@router.post("", status_code=201)
def create_user(body: UserFields, request: Request, actor: dict = Depends(require_superadmin)):
    data, error = _clean(body, creating=True)
    if error:
        return error
    password_hash, error = _password(body, required=True)
    if error:
        return error
    user = accounts.create_user(data, password_hash, str(actor["id"]))
    activity.record("user.create", user=actor, request=request, page="/users",
                    summary=f"Membuat akun {user['username']} ({user['role']})",
                    details={**_target(user), "email": user["email"], "branches": list(user.get("branch_codes") or [])})
    return JSONResponse({"user": accounts.public_user(user)}, status_code=201)


@router.get("/{user_id}")
def get_user(user_id: UUID):
    user = accounts.get_user(str(user_id))
    return {"user": accounts.public_user(user)} if user else _error("User tidak ditemukan", 404)


@router.patch("/{user_id}")
def update_user(user_id: UUID, body: UserFields, request: Request, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    data, error = _clean(body, creating=False, user_id=uid, current=current)
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
    changes = {k: {"from": _plain(current.get(accounts.EDITABLE.get(k, k))), "to": _plain(v)}
               for k, v in data.items() if k in accounts.EDITABLE and current.get(accounts.EDITABLE[k]) != v}
    if "branches" in data and sorted(current.get("branch_codes") or []) != data["branches"]:
        changes["branches"] = {"from": sorted(current.get("branch_codes") or []), "to": data["branches"]}
    activity.record("user.update", user=actor, request=request, page="/users",
                    summary=f"Mengubah akun {current['username']}" + (" (reset password)" if password_hash else ""),
                    details={**_target(current), "changes": changes, "passwordReset": bool(password_hash)})
    forget_sessions()
    return {"user": accounts.public_user(user)}


def _plain(value):
    return value if value is None or isinstance(value, (str, int, float, bool, list)) else str(value)


@router.post("/{user_id}/unlock")
def unlock_user(user_id: UUID, request: Request, actor: dict = Depends(require_superadmin)):
    user = accounts.unlock_user(str(user_id), str(actor["id"]))
    if user:
        activity.record("user.unlock", user=actor, request=request, page="/users",
                        summary=f"Membuka kunci akun {user['username']}", details=_target(user))
    return {"user": accounts.public_user(user)} if user else _error("User tidak ditemukan", 404)


@router.put("/{user_id}/avatar")
def set_avatar(user_id: UUID, body: AvatarUpload, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    if not accounts.get_user(uid):
        return _error("User tidak ditemukan", 404)
    parsed, problem = avatars.parse_data_url(body.image)
    if problem:
        return _error(problem, field="avatar")
    avatars.save(uid, *parsed, str(actor["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(uid))}


@router.delete("/{user_id}/avatar")
def delete_avatar(user_id: UUID, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    if not accounts.get_user(uid):
        return _error("User tidak ditemukan", 404)
    avatars.remove(uid, str(actor["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(uid))}


@router.delete("/{user_id}")
def delete_user(user_id: UUID, request: Request, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    if uid == str(actor["id"]):
        return _error("Anda tidak dapat menghapus akun Anda sendiri", 409)
    if current["role"] == "superadmin" and current["is_active"] and accounts.count_active_superadmins(exclude_id=uid) == 0:
        return _error("Minimal harus ada satu superadmin aktif", 409)
    accounts.delete_user(uid)
    activity.record("user.delete", user=actor, request=request, page="/users",
                    summary=f"Menghapus akun {current['username']}", details={**_target(current), "email": current["email"]})
    forget_sessions()
    return {"ok": True}
