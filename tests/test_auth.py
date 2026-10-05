"""Sign-in, sessions, roles and user management, against an in-memory account store."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import accounts, security
from app.main import app
from app.routes import auth as auth_module


def now():
    return datetime.now(timezone.utc)


class MemoryAccounts:
    """Same functions as app.accounts, kept in dicts."""

    def __init__(self):
        self.users: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}

    def add(self, username, email, full_name, password, role="user", is_active=True, **extra):
        uid = str(uuid.uuid4())
        self.users[uid] = {
            "id": uid, "username": username, "email": email, "full_name": full_name,
            "password_hash": security.hash_password(password), "role": role, "is_active": is_active,
            "phone_number": None, "job_title": None, "department": None, "notes": None,
            "must_change_password": False, "last_login_at": None, "last_login_ip": None,
            "failed_login_attempts": 0, "locked_until": None, "password_changed_at": now(),
            "created_at": now(), "updated_at": now(), "created_by_username": None, "updated_by_username": None, **extra,
        }
        return self.users[uid]

    # reads
    def find_for_login(self, identifier, method):
        key = "email" if method == "email" else "username"
        return next((u for u in self.users.values() if u[key].lower() == identifier.strip().lower()), None)

    def get_user(self, user_id):
        u = self.users.get(str(user_id))
        return dict(u) if u else None  # a snapshot, like a row read from the database

    def get_password_hash(self, user_id):
        return self.users[str(user_id)]["password_hash"]

    def list_users(self, search="", role="", status="", limit=20, offset=0, branch=""):
        rows = [u for u in self.users.values()
                if (not search or search.lower() in (u["username"] + u["email"] + u["full_name"]).lower())
                and (not branch or u["role"] == "superadmin" or branch in (u.get("branch_codes") or []))
                and (not role or u["role"] == role)
                and (status != "active" or u["is_active"]) and (status != "inactive" or not u["is_active"])]
        rows.sort(key=lambda u: u["full_name"] or u["username"])
        return rows[offset:offset + limit], len(rows)

    def exists(self, column, value, exclude_id=None):
        return any(u[column].lower() == value.lower() and u["id"] != exclude_id for u in self.users.values())

    def count_active_superadmins(self, exclude_id=None):
        return sum(1 for u in self.users.values() if u["role"] == "superadmin" and u["is_active"] and u["id"] != exclude_id)

    # writes
    def create_user(self, data, password_hash, actor_id):
        cols = {accounts.EDITABLE[k]: v for k, v in data.items() if k in accounts.EDITABLE}
        u = self.add(cols.pop("username"), cols.pop("email"), cols.pop("full_name", None), "x", **cols)
        u["password_hash"] = password_hash
        u["branch_codes"] = list(data.get("branches") or [])
        return u

    def update_user(self, user_id, data, actor_id, password_hash=None):
        u = self.users[user_id]
        u.update({accounts.EDITABLE[k]: v for k, v in data.items() if k in accounts.EDITABLE})
        if "branches" in data:
            u["branch_codes"] = list(data["branches"])
        if password_hash:
            u.update(password_hash=password_hash, failed_login_attempts=0, locked_until=None)
        if password_hash or data.get("isActive") is False or "role" in data:
            for s in self.sessions.values():
                if s["user_id"] == user_id:
                    s["revoked"] = True
        return u

    def unlock_user(self, user_id, actor_id):
        u = self.users.get(user_id)
        if u:
            u.update(failed_login_attempts=0, locked_until=None)
        return u

    def delete_user(self, user_id):
        return self.users.pop(user_id, None) is not None

    def record_login_failure(self, user_id):
        u = self.users[user_id]
        u["failed_login_attempts"] += 1
        if u["failed_login_attempts"] >= accounts.MAX_FAILED_LOGINS:
            u["locked_until"] = now() + timedelta(minutes=accounts.LOCK_MINUTES)
        return u

    def record_login_success(self, user_id, ip):
        self.users[user_id].update(last_login_at=now(), last_login_ip=ip, failed_login_attempts=0, locked_until=None)

    def create_session(self, user_id, token_hash, ttl, ip, user_agent):
        self.sessions[token_hash] = {"user_id": user_id, "expires": now() + ttl, "revoked": False}

    def session_user(self, token_hash):
        s = self.sessions.get(token_hash)
        if not s or s["revoked"] or s["expires"] < now():
            return None
        u = self.users.get(s["user_id"])
        return u if u and u["is_active"] else None

    def revoke_session(self, token_hash):
        if token_hash in self.sessions:
            self.sessions[token_hash]["revoked"] = True

    def revoke_other_sessions(self, user_id, keep_token_hash):
        for h, s in self.sessions.items():
            if s["user_id"] == user_id and h != keep_token_hash:
                s["revoked"] = True

    def update_profile(self, user_id, data):
        u = self.users[user_id]
        u.update({accounts.EDITABLE[k]: v for k, v in data.items() if k in accounts.EDITABLE})
        u["profile_updated_at"] = now()
        return u

    def set_own_password(self, user_id, password_hash):
        self.users[user_id].update(password_hash=password_hash, must_change_password=False)


@pytest.fixture
def store(monkeypatch, fake_db):
    mem = MemoryAccounts()
    for name in ("find_for_login", "get_user", "get_password_hash", "list_users", "exists", "count_active_superadmins",
                 "create_user", "update_user", "unlock_user", "delete_user", "record_login_failure",
                 "record_login_success", "create_session", "session_user", "revoke_session", "revoke_other_sessions",
                 "set_own_password", "update_profile"):
        monkeypatch.setattr(accounts, name, getattr(mem, name))
    auth_module.forget_sessions()
    mem.admin = mem.add("superadmin", "admin@kopicalf.co.id", "Super Admin", "Admin1234", role="superadmin")
    mem.user = mem.add("kasir", "kasir@kopicalf.co.id", "Kasir Satu", "Kasir1234", branch_codes=["CCI01"])
    return mem


@pytest.fixture
def anon(store):
    # https: the session cookie is Secure, as in production
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def login(c, identifier, password, method="username"):
    return c.post("/api/auth/login", json={"identifier": identifier, "password": password, "method": method})


def test_password_hashing():
    h = security.hash_password("Rahasia123")
    assert h.startswith("scrypt$") and security.verify_password("Rahasia123", h)
    assert not security.verify_password("rahasia123", h) and not security.verify_password("x", "garbage")
    assert security.password_problem("short1") and security.password_problem("onlyletters")
    assert security.password_problem("Valid1234") is None
    assert security.password_problem(security.generate_password()) is None


def test_data_endpoints_need_a_session(anon):
    assert anon.get("/api/overview/kpis").status_code == 401
    assert anon.get("/api/transactions").json()["error"]
    assert anon.get("/api/realtime/version").status_code == 401
    assert anon.get("/health").status_code in (200, 503)  # open


def test_login_with_username_and_email(anon, store):
    res = login(anon, "superadmin", "Admin1234")
    assert res.status_code == 200 and res.json()["user"]["role"] == "superadmin"
    cookie = res.headers["set-cookie"]
    assert "portal_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "password" not in str(res.json()).lower().replace("mustchangepassword", "").replace("passwordchangedat", "")
    assert anon.get("/api/auth/me").json()["user"]["username"] == "superadmin"
    assert store.admin["last_login_at"] is not None

    anon.post("/api/auth/logout")
    assert anon.get("/api/auth/me").status_code == 401
    res = login(anon, "KASIR@kopicalf.co.id", "Kasir1234", "email")
    assert res.status_code == 200 and res.json()["user"]["fullName"] == "Kasir Satu"
    # an email typed into the username option does not match
    assert login(anon, "kasir@kopicalf.co.id", "Kasir1234", "username").status_code == 401


def test_wrong_password_and_lockout(anon, store):
    for _ in range(accounts.MAX_FAILED_LOGINS - 1):
        res = login(anon, "kasir", "salah123")
        assert res.status_code == 401 and res.json()["error"] == "Username/email atau password salah"
    assert login(anon, "kasir", "salah123").status_code == 423
    assert login(anon, "kasir", "Kasir1234").status_code == 423  # locked even with the right password
    assert login(anon, "nobody", "whatever1").status_code == 401  # same answer for unknown users


def test_inactive_user_cannot_sign_in(anon, store):
    store.user["is_active"] = False
    assert login(anon, "kasir", "Kasir1234").status_code == 403


def test_user_role_has_no_user_management(anon, store):
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/users").status_code == 403
    assert anon.get("/api/overview/kpis?dateFrom=2026-09-01&dateTo=2026-09-02").status_code != 401


def test_user_crud(anon, store):
    login(anon, "superadmin", "Admin1234")
    body = {"username": "Budi.S", "email": "Budi@Kopicalf.co.id", "fullName": "  Budi   Santoso ", "role": "user",
            "password": "Budi12345", "phoneNumber": "0812-3456-789", "department": "Operations",
            "branches": ["TGP17", "CCI01", "CCI01"]}
    res = anon.post("/api/users", json=body)
    assert res.status_code == 201, res.text
    created = res.json()["user"]
    assert created["username"] == "budi.s" and created["email"] == "budi@kopicalf.co.id"
    assert created["fullName"] == "Budi Santoso" and created["department"] == "Operations"
    assert created["branches"] == ["CCI01", "TGP17"]
    uid = created["id"]

    # role "user" needs at least one known branch
    assert anon.post("/api/users", json={**body, "username": "nob", "email": "nob@kopicalf.co.id", "branches": []}).json()["field"] == "branches"
    missing = {k: v for k, v in body.items() if k != "branches"}
    assert anon.post("/api/users", json={**missing, "username": "nob", "email": "nob@kopicalf.co.id"}).json()["field"] == "branches"
    assert anon.post("/api/users", json={**body, "username": "nob", "email": "nob@kopicalf.co.id", "branches": ["XXX99"]}).json()["field"] == "branches"
    assert anon.patch(f"/api/users/{uid}", json={"branches": []}).json()["field"] == "branches"
    assert anon.patch(f"/api/users/{uid}", json={"branches": ["TGP17"]}).json()["user"]["branches"] == ["TGP17"]

    assert anon.post("/api/users", json={**body, "email": "x@kopicalf.co.id"}).json()["field"] == "username"
    assert anon.post("/api/users", json={**body, "username": "other", "email": "o@kopicalf.co.id", "password": "short"}).json()["field"] == "password"
    assert anon.post("/api/users", json={**body, "username": "other2"}).json()["field"] == "email"
    assert anon.post("/api/users", json={**body, "username": "bad name!"}).status_code == 422

    listed = anon.get("/api/users?search=budi").json()
    assert listed["total"] == 1 and listed["data"][0]["id"] == uid
    assert anon.get(f"/api/users/{uid}").json()["user"]["phoneNumber"] == "0812-3456-789"

    res = anon.patch(f"/api/users/{uid}", json={"role": "superadmin", "jobTitle": "Area Manager", "password": "Baru12345"})
    assert res.status_code == 200 and res.json()["user"]["role"] == "superadmin"
    assert res.json()["user"]["branches"] == []  # superadmins see every branch
    assert security.verify_password("Baru12345", store.users[uid]["password_hash"])

    assert anon.delete(f"/api/users/{uid}").json()["ok"] is True
    assert anon.get(f"/api/users/{uid}").status_code == 404


def test_safeguards(anon, store):
    login(anon, "superadmin", "Admin1234")
    me = store.admin["id"]
    assert anon.delete(f"/api/users/{me}").status_code == 409
    assert anon.patch(f"/api/users/{me}", json={"isActive": False}).status_code == 409
    assert anon.patch(f"/api/users/{me}", json={"role": "user", "branches": ["CCI01"]}).json()["field"] == "role"  # last superadmin


def test_role_change_signs_the_user_out(anon, store):
    other = TestClient(app, base_url="https://testserver")
    login(other, "kasir", "Kasir1234")
    assert other.get("/api/auth/me").status_code == 200
    login(anon, "superadmin", "Admin1234")
    anon.patch(f"/api/users/{store.user['id']}", json={"isActive": False})
    assert other.get("/api/auth/me").status_code == 401


def test_change_own_password(anon, store):
    login(anon, "kasir", "Kasir1234")
    assert anon.post("/api/auth/password", json={"currentPassword": "wrong", "newPassword": "Baru12345"}).status_code == 400
    assert anon.post("/api/auth/password", json={"currentPassword": "Kasir1234", "newPassword": "lemah"}).status_code == 422
    res = anon.post("/api/auth/password", json={"currentPassword": "Kasir1234", "newPassword": "Baru12345"})
    assert res.status_code == 200 and anon.get("/api/auth/me").status_code == 200  # this session stays
    anon.post("/api/auth/logout")
    assert login(anon, "kasir", "Baru12345").status_code == 200


# ---------------------------------------------------------------- profile pictures

import base64  # noqa: E402

from app import avatars  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def data_url(kind, data):
    return f"data:image/{kind};base64," + base64.b64encode(data).decode()


@pytest.fixture
def pictures(monkeypatch, store):
    saved = {}

    def save(user_id, content_type, data, actor_id):
        saved[user_id] = {"content_type": content_type, "data": data, "updated_at": now()}
        store.users[user_id]["avatar_updated_at"] = now()

    def remove(user_id, actor_id):
        saved.pop(user_id, None)
        store.users[user_id]["avatar_updated_at"] = None

    monkeypatch.setattr(avatars, "save", save)
    monkeypatch.setattr(avatars, "remove", remove)
    monkeypatch.setattr(avatars, "load", lambda user_id: saved.get(user_id))
    return saved


def test_avatar_validation():
    assert avatars.parse_data_url(data_url("png", PNG))[0] == ("image/png", PNG)
    assert avatars.parse_data_url(data_url("webp", WEBP))[0][0] == "image/webp"
    assert avatars.parse_data_url(data_url("jpeg", PNG))[1]  # declared type does not match the bytes
    assert avatars.parse_data_url("data:image/gif;base64,R0lGOD")[1]
    assert avatars.parse_data_url(data_url("png", PNG + b"\x00" * avatars.MAX_BYTES))[1]


def test_own_avatar(anon, store, pictures):
    login(anon, "kasir", "Kasir1234")
    res = anon.put("/api/auth/me/avatar", json={"image": data_url("webp", WEBP)})
    assert res.status_code == 200
    url = res.json()["user"]["avatarUrl"]
    assert url.startswith(f"/api/avatars/{store.user['id']}?v=")
    img = anon.get(url)
    assert img.status_code == 200 and img.content == WEBP and img.headers["content-type"] == "image/webp"
    assert "immutable" in img.headers["cache-control"]
    assert anon.put("/api/auth/me/avatar", json={"image": "data:text/plain;base64,aGk="}).status_code == 422
    assert anon.delete("/api/auth/me/avatar").json()["user"]["avatarUrl"] is None
    assert anon.get(url).status_code == 404


def test_admin_sets_other_avatar(anon, store, pictures):
    login(anon, "kasir", "Kasir1234")
    assert anon.put(f"/api/users/{store.admin['id']}/avatar", json={"image": data_url("png", PNG)}).status_code == 403
    anon.post("/api/auth/logout")
    login(anon, "superadmin", "Admin1234")
    res = anon.put(f"/api/users/{store.user['id']}/avatar", json={"image": data_url("png", PNG)})
    assert res.status_code == 200 and res.json()["user"]["avatarUrl"]
    assert anon.delete(f"/api/users/{store.user['id']}/avatar").json()["user"]["avatarUrl"] is None
    assert TestClient(app, base_url="https://testserver").get(f"/api/avatars/{store.user['id']}").status_code == 401



# ---------------------------------------------------------------- self-service profile

def test_admin_creates_login_only_and_user_completes_profile(anon, store, monkeypatch):
    from app import profile
    monkeypatch.setattr(profile, "branch_exists", lambda code: code == "CCI01")
    login(anon, "superadmin", "Admin1234")
    res = anon.post("/api/users", json={"username": "baru", "email": "baru@kopicalf.co.id", "password": "Baru12345", "branches": ["CCI01"]})
    assert res.status_code == 201, res.text
    created = res.json()["user"]
    assert created["fullName"] is None and created["displayName"] == "baru" and created["profileComplete"] is False
    anon.post("/api/auth/logout")

    login(anon, "baru", "Baru12345")
    me = "/api/auth/me"
    # cannot change the login itself
    assert anon.patch(me, json={"role": "superadmin"}).status_code == 422
    assert anon.patch(me, json={"username": "other"}).json()["field"] == "username"
    # validation
    assert anon.patch(me, json={"gender": "x"}).json()["field"] == "gender"
    assert anon.patch(me, json={"birthDate": "2999-01-01"}).json()["field"] == "birthDate"
    assert anon.patch(me, json={"phoneNumber": "abc"}).json()["field"] == "phoneNumber"
    assert anon.patch(me, json={"workBranchCode": "NOPE"}).json()["field"] == "workBranchCode"
    assert anon.patch(me, json={"fullName": "  "}).json()["field"] == "fullName"

    res = anon.patch(me, json={"fullName": " Budi  Baru ", "phoneNumber": "0812 3456 789", "jobTitle": "Barista",
                               "department": "Operations", "employeeNumber": "KC-0012", "gender": "male",
                               "birthDate": "1998-05-17", "address": "Jl. Supratman 1", "city": "Bandung",
                               "workBranchCode": "CCI01"})
    assert res.status_code == 200, res.text
    u = res.json()["user"]
    assert u["fullName"] == "Budi Baru" and u["displayName"] == "Budi Baru" and u["profileComplete"] is True
    assert u["birthDate"] == "1998-05-17" and u["workBranchCode"] == "CCI01" and u["profileUpdatedAt"]
    assert anon.get(me).json()["user"]["employeeNumber"] == "KC-0012"


# ---------------------------------------------------------------- branch scope (role "user")

def test_user_sees_only_assigned_branches(anon, store, fake_db):
    login(anon, "kasir", "Kasir1234")  # assigned to CCI01 only
    assert anon.get("/api/cost-control/meta").status_code == 403  # Cost Control: superadmin only
    assert [b["branch_code"] for b in anon.get("/api/branches").json()] == ["CCI01"]

    rows = anon.get("/api/transactions?dateFrom=2026-09-29&dateTo=2026-09-30&type=all").json()["data"]
    assert rows and {r["Branch Code"] if "Branch Code" in r else r.get("branchCode") for r in rows} <= {"CCI01", None}
    # asking for another branch never widens the scope
    fake_db.queries.clear()
    anon.get("/api/transactions?dateFrom=2026-09-29&dateTo=2026-09-30&type=all&branch=TGP17")
    assert any(p == ["-"] for _, params in fake_db.queries for p in (params or []) if isinstance(p, list))
    assert anon.get("/api/transactions/S-001").status_code == 404  # TGP17 transaction
    assert anon.get("/api/transactions/S-003").status_code == 200


def test_user_without_branches_sees_nothing(anon, store, fake_db):
    store.user["branch_codes"] = []
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/branches").json() == []
    assert anon.get("/api/transactions?dateFrom=2026-09-29&dateTo=2026-09-30&type=all").json()["data"] == []


def test_exports_belong_to_their_owner(anon, store, fake_db):
    other = TestClient(app, base_url="https://testserver")
    login(anon, "kasir", "Kasir1234")
    job = anon.post("/api/exports", json={"dateFrom": "2026-09-30", "dateTo": "2026-09-30", "type": "all"}).json()
    assert job["branch"] == "CCI01" and "owner" not in job
    assert [j["id"] for j in anon.get("/api/exports").json()["jobs"]] == [job["id"]]
    assert anon.get(f"/api/exports/{job['id']}").status_code == 200

    store.add("kasir2", "kasir2@kopicalf.co.id", "Kasir Dua", "Kasir1234", branch_codes=["TGP17"])
    login(other, "kasir2", "Kasir1234")
    assert other.get(f"/api/exports/{job['id']}").status_code == 404
    assert other.get(f"/api/exports/{job['id']}/download").status_code == 404
    assert other.get("/api/exports").json()["jobs"] == []
    login(other, "superadmin", "Admin1234")
    assert other.get(f"/api/exports/{job['id']}").status_code == 200


# ---------------------------------------------------------------- activity log

def test_activity_is_recorded(anon, store, fake_db):
    assert login(anon, "kasir", "salah").status_code == 401
    login(anon, "kasir", "Kasir1234")
    anon.get("/api/transactions/S-003")
    anon.get("/api/transactions/S-001")  # another branch
    job = anon.post("/api/exports", json={"dateFrom": "2026-09-30", "dateTo": "2026-09-30"}).json()
    anon.get(f"/api/exports/{job['id']}/download")
    anon.get("/api/users")
    assert anon.post("/api/activity/events", json={"action": "page.view", "page": "/sales"}).status_code == 204
    assert anon.post("/api/activity/events", json={"action": "user.delete"}).status_code == 422
    assert anon.get("/api/activity").status_code == 403  # superadmin only
    anon.post("/api/auth/logout")

    log = [(e["action"], e["status"], e["username"]) for e in fake_db.activity]
    assert log[:2] == [("auth.login_failed", "failed", "kasir"), ("auth.login", "ok", "kasir")]
    assert ("transaction.view", "ok", "kasir") in log and ("transaction.view", "denied", "kasir") in log
    assert ("export.create", "ok", "kasir") in log and ("export.done", "ok", "kasir") in log
    assert ("export.download", "ok", "kasir") in log
    assert ("access.denied", "denied", "kasir") in log and ("page.view", "ok", "kasir") in log
    assert log[-1] == ("auth.logout", "ok", "kasir")
    created = next(e for e in fake_db.activity if e["action"] == "export.create")
    assert created["details"]["branches"] == ["CCI01"] and created["details"]["dateFrom"] == "2026-09-30"


def test_user_changes_are_recorded(anon, store, fake_db):
    login(anon, "superadmin", "Admin1234")
    anon.patch(f"/api/users/{store.user['id']}", json={"branches": ["CCI01", "TGP17"], "password": "Baru12345"})
    entry = next(e for e in fake_db.activity if e["action"] == "user.update")
    assert entry["details"]["changes"]["branches"] == {"from": ["CCI01"], "to": ["CCI01", "TGP17"]}
    assert entry["details"]["passwordReset"] is True and "Baru12345" not in str(entry)


def test_create_with_identity_and_filter_by_branch(anon, store):
    login(anon, "superadmin", "Admin1234")
    body = {"username": "okta.fajri", "email": "okta@kopicalf.co.id", "password": "Okta12345", "role": "user",
            "branches": ["TGP17"], "fullName": "Okta Fajri", "phoneNumber": "0812-3456-7890", "jobTitle": "PIC Outlet",
            "department": "Operations", "notes": "Area Tangerang"}
    created = anon.post("/api/users", json=body).json()["user"]
    assert created["fullName"] == "Okta Fajri" and created["jobTitle"] == "PIC Outlet" and created["profileComplete"]
    assert created["notes"] == "Area Tangerang" and created["branches"] == ["TGP17"]
    names = {u["username"] for u in anon.get("/api/users?branch=TGP17").json()["data"]}
    assert names == {"okta.fajri", "superadmin"}  # kasir (CCI01) not listed


def test_users_summary_is_superadmin_only(anon, store):
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/users/summary").status_code == 403
    login(anon, "superadmin", "Admin1234")
    body = anon.get("/api/users/summary").json()
    assert {"total", "active", "locked", "superadmins", "users", "withoutBranch", "active7d", "branchesCovered"} <= set(body)
