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
"""
import re
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import accounts, avatars
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


def _error(message: str, status: int = 422, field: Optional[str] = None) -> JSONResponse:
    return JSONResponse({"error": message, "field": field}, status_code=status)


def _clean(body: UserFields, creating: bool, user_id: Optional[str] = None) -> tuple[Optional[dict], Optional[JSONResponse]]:
    """Normalised fields to store, or a validation error response."""
    data = body.model_dump(exclude_unset=True)
    data.pop("password", None)
    for key in ("phoneNumber", "jobTitle", "department", "notes"):
        if key in data:
            data[key] = (data[key] or "").strip() or None
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
    if "fullName" in data:
        data["fullName"] = re.sub(r"\s+", " ", data["fullName"] or "").strip()
        if not data["fullName"]:
            return None, _error("Nama lengkap wajib diisi", field="fullName")
    if "phoneNumber" in data and data["phoneNumber"] and not re.fullmatch(r"[0-9+()\-\s]{6,32}", data["phoneNumber"]):
        return None, _error("Nomor telepon tidak valid", field="phoneNumber")
    if creating:
        for key, label in (("username", "Username"), ("email", "Email"), ("fullName", "Nama lengkap")):
            if not data.get(key):
                return None, _error(f"{label} wajib diisi", field=key)
        data.setdefault("role", "user")
        data.setdefault("isActive", True)
    return data, None


def _password(body: UserFields, required: bool) -> tuple[Optional[str], Optional[JSONResponse]]:
    if not body.password:
        return None, (_error("Password wajib diisi", field="password") if required else None)
    problem = password_problem(body.password)
    if problem:
        return None, _error(problem, field="password")
    return hash_password(body.password), None


@router.get("")
def list_users(search: str = "", role: str = "", status: str = "",
               page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100)):
    rows, total = accounts.list_users(search, role, status, pageSize, (page - 1) * pageSize)
    return {"data": [accounts.public_user(r) for r in rows], "total": total, "page": page, "pageSize": pageSize}


@router.post("", status_code=201)
def create_user(body: UserFields, actor: dict = Depends(require_superadmin)):
    data, error = _clean(body, creating=True)
    if error:
        return error
    password_hash, error = _password(body, required=True)
    if error:
        return error
    user = accounts.create_user(data, password_hash, str(actor["id"]))
    return JSONResponse({"user": accounts.public_user(user)}, status_code=201)


@router.get("/{user_id}")
def get_user(user_id: UUID):
    user = accounts.get_user(str(user_id))
    return {"user": accounts.public_user(user)} if user else _error("User tidak ditemukan", 404)


@router.patch("/{user_id}")
def update_user(user_id: UUID, body: UserFields, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    data, error = _clean(body, creating=False, user_id=uid)
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
    forget_sessions()
    return {"user": accounts.public_user(user)}


@router.post("/{user_id}/unlock")
def unlock_user(user_id: UUID, actor: dict = Depends(require_superadmin)):
    user = accounts.unlock_user(str(user_id), str(actor["id"]))
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
def delete_user(user_id: UUID, actor: dict = Depends(require_superadmin)):
    uid = str(user_id)
    current = accounts.get_user(uid)
    if not current:
        return _error("User tidak ditemukan", 404)
    if uid == str(actor["id"]):
        return _error("Anda tidak dapat menghapus akun Anda sendiri", 409)
    if current["role"] == "superadmin" and current["is_active"] and accounts.count_active_superadmins(exclude_id=uid) == 0:
        return _error("Minimal harus ada satu superadmin aktif", 409)
    accounts.delete_user(uid)
    forget_sessions()
    return {"ok": True}
