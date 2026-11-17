"""Sign-in, sign-out and the current user; access dependencies for every other route.

The browser reaches the API through the dashboard's own origin (Next.js
/api/* proxy), so the session lives in an HttpOnly, Secure, SameSite=Lax cookie
that scripts cannot read. API clients may send the same token as
"Authorization: Bearer <token>".

    POST /api/auth/login     {"identifier", "password", "method": "username"|"email", "remember"}
    POST /api/auth/logout
    GET  /api/auth/me
    PUT   /api/auth/me/menu  {"hidden": [page]} pages left out of the own sidebar (display only, migration 054)
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

from app import access, accounts, activity, avatars
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
    access.fresh()
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
    if _from_portal(request) and user.get("role") == "driver":
        raise HTTPException(status_code=403, detail=COTR_ONLY)
    if _from_portal(request) and access.operations_only(user):
        raise HTTPException(status_code=403, detail=OPERATIONS_ONLY)
    return user


OPERATIONS_ONLY = "Akun ini menggunakan aplikasi Operations. Silakan login di operations.kopicalf.co.id"
COTR_ONLY = "Akun driver menggunakan aplikasi Calf On The Road. Silakan login di cotr.kopicalf.co.id"


def _from_portal(request: Request) -> bool:
    """Is the call made by the portal (portal.kopicalf.co.id)? Operations, scripts and API clients send no header."""
    return request.headers.get(access.APP_HEADER, "").strip().lower() == "portal"


def require_account_admin(request: Request, user: dict = Depends(current_user)) -> dict:
    """User Accounts (2026-11-13): a superadmin, an admin (accounts of role User) or a COTR admin (the COTR line)."""
    if not access.account_admin(user):
        activity.record("access.denied", user=user, request=request, status="denied",
                        summary=f"Akses ditolak: {request.method} {request.url.path}",
                        details={"method": request.method, "path": request.url.path, "query": str(request.url.query)})
        raise HTTPException(status_code=403, detail="Hanya superadmin atau admin yang dapat mengelola akun")
    return user


def require_superadmin(request: Request, user: dict = Depends(current_user)) -> dict:
    if user["role"] != "superadmin":
        activity.record("access.denied", user=user, request=request, status="denied",
                        summary=f"Akses ditolak: {request.method} {request.url.path}",
                        details={"method": request.method, "path": request.url.path, "query": str(request.url.query)})
        raise HTTPException(status_code=403, detail="Hanya superadmin yang dapat mengakses fitur ini")
    return user


def require_roles(*roles: str):
    """Dependency: the signed-in user has one of `roles` (refusals are logged like require_superadmin)."""

    def check(request: Request, user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            activity.record("access.denied", user=user, request=request, status="denied",
                            summary=f"Akses ditolak: {request.method} {request.url.path}",
                            details={"method": request.method, "path": request.url.path, "query": str(request.url.query)})
            raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke fitur ini")
        return user

    return check


def signed_in_user(row: dict) -> dict:
    """The user as the app keeps it after sign-in: the account plus the pages it opens."""
    # platforms: {page: [portal | operations | cotr]} where the superadmins set the menus (display only, 054)
    out = {**accounts.public_user(row), "pages": access.pages_for(row), "platforms": access.page_platforms()}
    # 2026-11-12 (#16): the pool / HUB a COTR storage or pool leader account is bound to (the pool filters lock on it)
    try:
        from app import cotr
        out["hubPool"] = cotr.own_pool(row)
    except Exception:  # noqa: BLE001 - never block the sign-in on it
        out["hubPool"] = None
    # 2026-11-11: no SWAT menu of its own any more; every app shows the pages given to the account (User Accounts)
    return out


def forget_sessions() -> None:
    """Drop cached session lookups (after a user or role change), on every worker (access.bump, migration 111)."""
    _sessions._data.clear()
    access.bump()


access._drop_hooks.append(_sessions._data.clear)


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
    if _from_portal(request) and row["role"] == "driver":
        failed("cotr_only", "Login portal ditolak: akun driver Calf On The Road")
        return JSONResponse({"error": COTR_ONLY, "redirect": settings.cotr_url}, status_code=403)
    if _from_portal(request) and access.operations_only(row):
        # form accounts sign in to Operations (2026-10-28); no session is made here
        failed("operations_only", "Login portal ditolak: akun menggunakan Operations")
        return JSONResponse({"error": OPERATIONS_ONLY, "redirect": settings.operations_url}, status_code=403)

    ttl = timedelta(days=settings.session_remember_days) if req.remember else timedelta(hours=settings.session_hours)
    token = new_token()
    ip = request.client.host if request.client else None
    accounts.create_session(str(row["id"]), token_hash(token), ttl, ip, request.headers.get("user-agent"))
    accounts.record_login_success(str(row["id"]), ip)
    logger.info("login %s (%s) from %s", row["username"], row["role"], ip)
    activity.record("auth.login", user=row, request=request, summary="Login",
                    details={"method": req.method, "remember": req.remember})
    response = JSONResponse({"user": signed_in_user(accounts.get_user(str(row["id"])))})
    _set_cookie(response, token, int(ttl.total_seconds()))
    return response


class HrmsExchange(BaseModel):
    accessToken: str = Field(min_length=10, max_length=4000)


NOT_LINKED = "Akun HRMS Anda belum terhubung ke akun portal. Hubungi admin untuk menautkannya."


def hrms_employee_of(access_token: str) -> Optional[str]:
    """The HRMS employee id of an HRMS access token, asked to the HRMS API (it refuses an expired token or an inactive
    user); None when HRMS refuses it. Raises HTTPException 503 when HRMS cannot be reached."""
    import requests
    try:
        r = requests.get(f"{get_settings().hrms_api_url.rstrip('/')}/api/v1/auth/me",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=8)
    except requests.RequestException as exc:
        logger.warning("hrms exchange: HRMS unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="HRMS tidak dapat dihubungi, coba lagi sebentar.") from None
    if r.status_code != 200:
        return None
    emp = (r.json() or {}).get("employee_id")
    return str(emp) if emp not in (None, "") else None


@router.post("/hrms-exchange")
def hrms_exchange(body: HrmsExchange, request: Request):
    """The attendance app (kopicalf-attandance, 2026-11-17): its HRMS token for a portal session of the person account
    linked to the same employee (user_account.hrms_employee_id, account_kind person, active). The forms it sends are
    then owned and scored like those of Operations. No password is involved; the session is short (session_hours) and
    returned as a Bearer token (the app sends X-Kopicalf-App: attendance)."""
    employee = hrms_employee_of(body.accessToken)
    if not employee:
        return JSONResponse({"error": "Sesi HRMS tidak valid, silakan login ulang."}, status_code=401)
    row = accounts.find_by_hrms_employee(employee)
    if not row or not row["is_active"]:
        activity.record("auth.login_failed", user=row, request=request, status="failed",
                        summary="Login aplikasi attendance ditolak: karyawan belum terhubung ke akun portal",
                        details={"reason": "not_linked" if not row else "inactive", "hrmsEmployeeId": employee})
        return JSONResponse({"error": NOT_LINKED if not row else "Akun portal Anda dinonaktifkan. Hubungi administrator.",
                             "code": "not_linked" if not row else "inactive"}, status_code=403)
    ttl = timedelta(hours=get_settings().session_hours)
    token = new_token()
    ip = request.client.host if request.client else None
    accounts.create_session(str(row["id"]), token_hash(token), ttl, ip, request.headers.get("user-agent"))
    activity.record("auth.login", user=row, request=request, summary="Login aplikasi attendance (HRMS)",
                    details={"method": "hrms", "app": "attendance"})
    return {"token": token, "expiresIn": int(ttl.total_seconds()), "user": signed_in_user(accounts.get_user(str(row["id"])))}


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
    return {"user": signed_in_user(user)}


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
    if user.get("hrms_employee_id"):  # the profile follows HRMS (migration 110, app/hrms_sync.py SYNCED)
        from app.hrms_sync import locked
        for k in locked(user):
            data.pop(k, None)
    if "fullName" in data and not data["fullName"]:
        return JSONResponse({"error": "Nama lengkap wajib diisi", "field": "fullName"}, status_code=422)
    updated = accounts.update_profile(str(user["id"]), data)
    activity.record("profile.update", user=user, request=request, summary="Memperbarui profil sendiri",
                    details={"fields": sorted(data)})
    forget_sessions()
    return {"user": signed_in_user(updated)}


@router.put("/me/menu")
def update_my_menu(body: dict, request: Request, user: dict = Depends(current_user)):
    """The pages the user leaves out of the own sidebar (migration 054); display only, the access stays."""
    hidden = access.clean_hidden(body.get("hidden"))
    updated = accounts.update_user(str(user["id"]), {"hiddenPages": hidden}, str(user["id"]))
    activity.record("profile.menu", user=user, request=request, summary="Mengatur tampilan menu", details={"hidden": hidden})
    forget_sessions()
    return {"user": signed_in_user(updated)}


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
    return {"user": signed_in_user(accounts.get_user(str(user["id"])))}


@router.put("/me/avatar")
def set_my_avatar(body: AvatarUpload, user: dict = Depends(current_user)):
    parsed, problem = avatars.parse_data_url(body.image)
    if problem:
        return JSONResponse({"error": problem}, status_code=422)
    avatars.save(str(user["id"]), *parsed, str(user["id"]))
    forget_sessions()
    return {"user": signed_in_user(accounts.get_user(str(user["id"])))}


@router.delete("/me/avatar")
def delete_my_avatar(user: dict = Depends(current_user)):
    avatars.remove(str(user["id"]), str(user["id"]))
    forget_sessions()
    return {"user": signed_in_user(accounts.get_user(str(user["id"])))}


def _now() -> datetime:
    return datetime.now(timezone.utc)
