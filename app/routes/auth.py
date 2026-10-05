"""Sign-in, sign-out and the current user; access dependencies for every other route.

The browser reaches the API through the dashboard's own origin (Next.js
/api/* proxy), so the session lives in an HttpOnly, Secure, SameSite=Lax cookie
that scripts cannot read. API clients may send the same token as
"Authorization: Bearer <token>".

    POST /api/auth/login     {"identifier", "password", "method": "username"|"email", "remember"}
    POST /api/auth/logout
    GET  /api/auth/me
    PATCH /api/auth/me       own profile: fullName, phoneNumber, jobTitle, department, employeeNumber,
                             gender, birthDate, address, city, workBranchCode (not username/email/role)
    POST /api/auth/password  {"currentPassword", "newPassword"}
    PUT  /api/auth/me/avatar {"image": "data:image/webp;base64,..."}   DELETE /api/auth/me/avatar
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import accounts, activity, avatars
from app.profile import PROFILE_FIELDS, ProfileError, clean_profile
from app.config import get_settings
from app.security import DUMMY_HASH, hash_password, new_token, password_problem, token_hash, verify_password
from app.utils import TTLCache

logger = logging.getLogger("auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "portal_session"
SESSION_CACHE_TTL = 20  # seconds; sign-out clears it at once on this worker
_sessions = TTLCache()


# ---------------------------------------------------------------- dependencies

def _token(request: Request) -> Optional[str]:
    token = request.cookies.get(COOKIE)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return token or None


def session_lookup(request: Request) -> Optional[dict]:
    """The user behind the request's session (cached briefly), or None. Never raises."""
    token = _token(request)
    if not token:
        return None
    key = token_hash(token)
    user = _sessions.get(key)
    if user is None:
        user = accounts.session_user(key)
        if user:
            _sessions.set(key, user, SESSION_CACHE_TTL)
    return user or None


def current_user(request: Request) -> dict:
    """The signed-in user (raw row), or 401."""
    if not _token(request):
        raise HTTPException(status_code=401, detail="Silakan login terlebih dahulu")
    user = session_lookup(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sesi berakhir, silakan login kembali")
    request.state.user = user
    return user


def require_superadmin(request: Request, user: dict = Depends(current_user)) -> dict:
    if user["role"] != "superadmin":
        activity.record("access.denied", user=user, request=request, status="denied",
                        summary=f"Akses ditolak: {request.method} {request.url.path}",
                        details={"method": request.method, "path": request.url.path, "query": str(request.url.query)})
        raise HTTPException(status_code=403, detail="Hanya superadmin yang dapat mengakses fitur ini")
    return user


def forget_sessions() -> None:
    """Drop cached session lookups (after a user or role change)."""
    _sessions._data.clear()


# ---------------------------------------------------------------- routes

class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    method: Literal["username", "email"] = "username"
    remember: bool = False


class AvatarUpload(BaseModel):
    image: str = Field(min_length=1, max_length=800_000)  # data URL, <= 512 KB of image


class PasswordChange(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=1, max_length=128)


def _set_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(COOKIE, token, max_age=max_age, httponly=True, secure=True, samesite="lax", path="/")


@router.post("/login")
def login(req: LoginRequest, request: Request):
    settings = get_settings()
    row = accounts.find_for_login(req.identifier, req.method)
    # same work whether or not the account exists (no account enumeration by timing)
    password_ok = verify_password(req.password, row["password_hash"] if row else DUMMY_HASH)
    wrong = {"error": "Username/email atau password salah"}

    def failed(reason: str, summary: str) -> None:
        activity.record("auth.login_failed", user=row, request=request, status="failed", summary=summary,
                        username=None if row else req.identifier.strip()[:80],
                        details={"reason": reason, "method": req.method, "identifier": req.identifier.strip()[:80]})

    if not row:
        failed("unknown_account", "Login gagal: akun tidak ditemukan")
        return JSONResponse(wrong, status_code=401)
    if row["locked_until"] is not None and row["locked_until"] > _now():
        failed("locked", "Login ditolak: akun sedang terkunci")
        minutes = max(1, int((row["locked_until"] - _now()).total_seconds() // 60) + 1)
        return JSONResponse({"error": f"Akun terkunci karena terlalu banyak percobaan. Coba lagi dalam {minutes} menit."},
                            status_code=423)
    if not password_ok:
        state = accounts.record_login_failure(str(row["id"]))
        left = accounts.MAX_FAILED_LOGINS - (state["failed_login_attempts"] if state else 0)
        failed("wrong_password", "Login gagal: password salah" + (" - akun dikunci" if left <= 0 else ""))
        if left <= 0:
            return JSONResponse({"error": f"Akun terkunci selama {accounts.LOCK_MINUTES} menit karena terlalu banyak percobaan."},
                                status_code=423)
        return JSONResponse(wrong, status_code=401)
    if not row["is_active"]:
        failed("inactive", "Login ditolak: akun nonaktif")
        return JSONResponse({"error": "Akun dinonaktifkan. Hubungi administrator."}, status_code=403)

    ttl = timedelta(days=settings.session_remember_days) if req.remember else timedelta(hours=settings.session_hours)
    token = new_token()
    ip = request.client.host if request.client else None
    accounts.create_session(str(row["id"]), token_hash(token), ttl, ip, request.headers.get("user-agent"))
    accounts.record_login_success(str(row["id"]), ip)
    logger.info("login %s (%s) from %s", row["username"], row["role"], ip)
    activity.record("auth.login", user=row, request=request, summary="Login",
                    details={"method": req.method, "remember": req.remember})
    response = JSONResponse({"user": accounts.public_user(accounts.get_user(str(row["id"])))})
    _set_cookie(response, token, int(ttl.total_seconds()))
    return response


@router.post("/logout")
def logout(request: Request):
    token = _token(request)
    if token:
        user = session_lookup(request)
        if user:
            activity.record("auth.logout", user=user, request=request, summary="Logout")
        accounts.revoke_session(token_hash(token))
        _sessions._data.pop(token_hash(token), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return {"user": accounts.public_user(user)}


@router.patch("/me")
def update_me(body: dict, request: Request, user: dict = Depends(current_user)):
    unknown = sorted(set(body) - set(PROFILE_FIELDS))
    if unknown:
        return JSONResponse({"error": f"Field tidak dapat diubah sendiri: {', '.join(unknown)}", "field": unknown[0]},
                            status_code=422)
    try:
        data = clean_profile(body)
    except ProfileError as exc:
        return JSONResponse({"error": str(exc), "field": exc.field}, status_code=422)
    if "fullName" in data and not data["fullName"]:
        return JSONResponse({"error": "Nama lengkap wajib diisi", "field": "fullName"}, status_code=422)
    updated = accounts.update_profile(str(user["id"]), data)
    activity.record("profile.update", user=user, request=request, summary="Memperbarui profil sendiri",
                    details={"fields": sorted(data)})
    forget_sessions()
    return {"user": accounts.public_user(updated)}


@router.post("/password")
def change_password(req: PasswordChange, request: Request, user: dict = Depends(current_user)):
    if not verify_password(req.currentPassword, accounts.get_password_hash(str(user["id"]))):
        return JSONResponse({"error": "Password saat ini salah"}, status_code=400)
    problem = password_problem(req.newPassword)
    if problem:
        return JSONResponse({"error": problem}, status_code=422)
    if req.newPassword == req.currentPassword:
        return JSONResponse({"error": "Password baru harus berbeda dari password saat ini"}, status_code=422)
    accounts.set_own_password(str(user["id"]), hash_password(req.newPassword))
    # stay signed in here, sign out everywhere else
    keep = token_hash(_token(request) or "")
    accounts.revoke_other_sessions(str(user["id"]), keep)
    activity.record("auth.password_change", user=user, request=request, summary="Mengganti password sendiri")
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(str(user["id"])))}


@router.put("/me/avatar")
def set_my_avatar(body: AvatarUpload, user: dict = Depends(current_user)):
    parsed, problem = avatars.parse_data_url(body.image)
    if problem:
        return JSONResponse({"error": problem}, status_code=422)
    avatars.save(str(user["id"]), *parsed, str(user["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(str(user["id"])))}


@router.delete("/me/avatar")
def delete_my_avatar(user: dict = Depends(current_user)):
    avatars.remove(str(user["id"]), str(user["id"]))
    forget_sessions()
    return {"user": accounts.public_user(accounts.get_user(str(user["id"])))}


def _now() -> datetime:
    return datetime.now(timezone.utc)
