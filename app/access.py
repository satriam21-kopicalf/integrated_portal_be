"""Page access and the job title / department lists of User Accounts (migration 012).

A page is a menu entry of the dashboard (PAGES). Each role opens the pages in integration_portal.role_page (superadmins
change them in User Accounts → Roles & pages); one user may get pages added or removed on top of the role
(integration_portal.user_page). Superadmins always open every page. User Accounts and Activity Logs are not pages of
the catalog: they stay superadmin-only, so nobody can hand out access to themselves.

The routes use require_page(); the stock opname gateway maps every /api/so/... path to the page that uses it
(so_page()), so the backend enforces the same as the menu.
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request

from app import activity
from app import database as db
from app.utils import TTLCache

T_ROLE_PAGE = "integration_portal.role_page"
T_USER_PAGE = "integration_portal.user_page"
MASTERS = {"jobTitle": "integration_portal.job_title", "department": "integration_portal.department"}

# key, label, group (same order as the menu); the frontend has the same catalog (src/lib/pages.ts)
PAGES: list[tuple[str, str, str]] = [
    ("overview", "Dashboard", "Main menu"),
    ("sales", "Sales Transactions", "Main menu"),
    ("stock-opname", "Stock Opname (count)", "Main menu"),
    ("waste", "Waste (input)", "Main menu"),
    ("guide", "Guide", "Main menu"),
    ("form.pic-check-in", "PIC Check In (form)", "Form"),
    ("form.product", "Monitoring Product (form)", "Form"),
    ("form.outlet", "Monitoring Outlet (form)", "Form"),
    ("form.tools", "Tools & Heavy Tools (form)", "Form"),
    ("cc.monitor", "Monitor", "Cost Control · Stock opname"),
    ("cc.reviews", "Review", "Cost Control · Stock opname"),
    ("cc.entry", "Enter / import SO", "Cost Control · Stock opname"),
    ("cc.reports", "Report Stock Opname", "Cost Control · Stock opname"),
    ("cc.esb-export", "ESB export", "Cost Control · ESB"),
    ("cc.esb-compare", "ESB comparison", "Cost Control · ESB"),
    ("cc.lists", "Count Lists", "Cost Control · Stock opname settings"),
    ("cc.groups", "Items Groups", "Cost Control · Stock opname settings"),
    ("cc.types", "Count Types", "Cost Control · Stock opname settings"),
    ("cc.premix", "Premix calculator", "Cost Control · Stock opname settings"),
    ("cc.locations", "Outlet locations", "Cost Control · Stock opname settings"),
    ("cc.waste-monitor", "Waste monitor", "Cost Control · Waste"),
    ("cc.waste-review", "Waste review", "Cost Control · Waste"),
    ("cc.ratio", "Outlet ratio", "Cost Control · Analytics"),
    ("cc.cogs", "COGS & usage", "Cost Control · Analytics"),
    ("mon.overview", "Overview", "Monitoring"),
    ("mon.pic-check-in", "PIC Check In", "Monitoring"),
    ("mon.product", "Monitoring Product", "Monitoring"),
    ("mon.outlet", "Monitoring Outlet", "Monitoring"),
    ("mon.tools", "Tools & Heavy Tools", "Monitoring"),
    ("mon.branch-groups", "Branch groups", "Monitoring"),
    ("rating.google-maps", "Google Maps", "Rating & Review"),
    ("rating.shopeefood", "ShopeeFood", "Rating & Review"),
    ("rating.gofood", "GoFood", "Rating & Review"),
    ("rating.grabfood", "GrabFood", "Rating & Review"),
]
PAGE_KEYS = tuple(p[0] for p in PAGES)
CC_PAGES = tuple(k for k in PAGE_KEYS if k.startswith("cc."))
EDITABLE_ROLES = ("cost_control", "user")
DEFAULT_ROLE_PAGES = {
    "user": ["overview", "sales", "stock-opname", "guide", "form.pic-check-in", "form.product", "form.outlet"],
    "cost_control": ["overview", "sales", "stock-opname", "waste", "guide", *CC_PAGES],
}

_cache = TTLCache()


def role_pages() -> dict[str, list[str]]:
    """{role: [page, ...]} (cached 30 s; cleared on change)."""
    hit = _cache.get("roles")
    if hit is None:
        hit = {r: [] for r in EDITABLE_ROLES}
        for row in db.fetch(f"SELECT role, page FROM {T_ROLE_PAGE} ORDER BY role, page"):
            if row["page"] in PAGE_KEYS:
                hit.setdefault(row["role"], []).append(row["page"])
        _cache.set("roles", hit, 30)
    return hit


def user_overrides(user_id: str) -> dict[str, bool]:
    """{page: allowed} exceptions of one user."""
    key = f"user:{user_id}"
    hit = _cache.get(key)
    if hit is None:
        hit = {r["page"]: r["allowed"] for r in db.fetch(
            f"SELECT page, allowed FROM {T_USER_PAGE} WHERE user_id = %s", (user_id,)) if r["page"] in PAGE_KEYS}
        _cache.set(key, hit, 30)
    return hit


def pages_for(user: dict) -> list[str]:
    """Pages the user opens, in menu order."""
    if user.get("role") == "superadmin":
        return list(PAGE_KEYS)
    base = set(role_pages().get(user.get("role") or "user", []))
    for page, allowed in user_overrides(str(user["id"])).items():
        (base.add if allowed else base.discard)(page)
    return [k for k in PAGE_KEYS if k in base]


def has_page(user: dict, page: str) -> bool:
    return page in pages_for(user)


def deny(request: Request, user: dict) -> None:
    activity.record("access.denied", user=user, request=request, status="denied",
                    summary=f"Akses ditolak: {request.method} {request.url.path}",
                    details={"method": request.method, "path": request.url.path, "query": str(request.url.query)})
    raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke halaman ini")


def require_page(*pages: str):
    """Dependency: the signed-in user opens at least one of `pages` (refusals are logged)."""
    from app.routes.auth import current_user  # app.routes.auth imports this module

    def check(request: Request, user: dict = Depends(current_user)) -> dict:
        if not any(has_page(user, p) for p in pages):
            deny(request, user)
        return user

    return check


# ---------------------------------------------------------------- stock opname paths → pages

# first match wins: (method or None, path prefix after /api/so/, pages any of which opens it)
SO_RULES: list[tuple[Optional[str], str, tuple[str, ...]]] = [
    (None, "counts/today", ("stock-opname",)),
    (None, "counts/monitor", ("cc.monitor",)),
    ("POST", "counts/review", ("cc.reviews",)),
    ("POST", "counts/entries", ("cc.entry",)),
    ("POST", "counts/imports", ("cc.entry",)),
    (None, "count-reports", ("cc.reports", "cc.reviews")),
    ("GET", "count-settings/types", ("stock-opname", *CC_PAGES)),  # names of the count types on every SO screen
    ("POST", "count-settings/types/preview", ("cc.types",)),
    (None, "count-settings/types", ("cc.types",)),
    (None, "count-settings/calendar", ("cc.types",)),
    (None, "count-settings/locations", ("cc.locations",)),  # coordinates & radius of the facilities (06.9 §12.15)
    (None, "count-settings", ("cc.lists", "cc.groups", "cc.types")),
    (None, "count-exports", ("cc.esb-export",)),
    (None, "count-compare", ("cc.esb-compare",)),
    ("GET", "recipes", ("cc.premix", "stock-opname", "cc.entry")),
    (None, "recipes", ("cc.premix",)),
    ("GET", "counts", ("stock-opname", "cc.monitor", "cc.reviews", "cc.entry", "cc.reports", "cc.esb-export", "cc.esb-compare")),
    (None, "counts", ("stock-opname", "cc.monitor", "cc.reviews", "cc.entry", "cc.esb-export", "cc.esb-compare")),
    (None, "count-photos", ("stock-opname", "cc.monitor", "cc.reviews", "cc.entry")),
]


def so_page(method: str, path: str) -> tuple[str, ...]:
    """Pages that open the stock opname API path (relative to /api/so/); () = nobody but superadmins."""
    path = path.strip("/")
    if path.startswith("counts/") and path.endswith("/review") and method == "POST":
        return ("cc.reviews",)
    if path.startswith("counts/") and path.endswith("/compare"):
        return ("cc.esb-compare",)
    for m, prefix, pages in SO_RULES:
        if (m is None or m == method) and (path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")):
            return pages
    return ()


def so_role(user: dict) -> str:
    """Role the gateway assertion carries: a "user" given Cost Control pages acts as Cost Control in kopicalf (the
    paths it may call are limited by so_page)."""
    role = user.get("role") or "user"
    if role == "user" and any(p in CC_PAGES for p in pages_for(user)):
        return "cost_control"
    return role


# ---------------------------------------------------------------- changes (superadmin)

def set_role_pages(role: str, pages: list[str], actor_id: Optional[str]) -> None:
    if role not in EDITABLE_ROLES:
        raise ValueError("Role tidak dapat diubah")
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {T_ROLE_PAGE} WHERE role = %s", (role,))
        for page in pages:
            conn.execute(f"INSERT INTO {T_ROLE_PAGE} (role, page, updated_by) VALUES (%s, %s, %s)", (role, page, actor_id))
    _cache._data.clear()


def set_user_overrides(user_id: str, overrides: dict[str, bool], actor_id: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {T_USER_PAGE} WHERE user_id = %s", (user_id,))
        for page, allowed in overrides.items():
            conn.execute(f"INSERT INTO {T_USER_PAGE} (user_id, page, allowed, updated_by) VALUES (%s, %s, %s, %s)",
                         (user_id, page, bool(allowed), actor_id))
    _cache._data.clear()


def clean_overrides(role: str, overrides: dict) -> dict[str, bool]:
    """Keeps only real exceptions (a page the role already has is not "added", and so on)."""
    if role == "superadmin":
        return {}
    unknown = sorted(set(overrides) - set(PAGE_KEYS))
    if unknown:
        raise ValueError(f"Halaman tidak dikenal: {', '.join(unknown)}")
    base = set(role_pages().get(role, []))
    return {p: bool(a) for p, a in overrides.items() if bool(a) != (p in base)}


# ---------------------------------------------------------------- job titles & departments

def list_master(kind: str) -> list[dict]:
    t = MASTERS[kind]
    col = "job_title" if kind == "jobTitle" else "department"
    return [{"id": str(r["id"]), "name": r["name"], "isActive": r["is_active"], "users": r["users"]} for r in db.fetch(
        f"""SELECT m.id, m.name, m.is_active,
                   (SELECT count(*)::int FROM integration_portal.user_account u
                    WHERE lower(btrim(u.{col})) = lower(btrim(m.name))) AS users
            FROM {t} m ORDER BY m.is_active DESC, lower(m.name)""")]


def add_master(kind: str, name: str, actor_id: Optional[str]) -> dict:
    """Adds a name (or returns the existing one, case-insensitive; a deactivated one is reactivated)."""
    t = MASTERS[kind]
    name = " ".join(name.split())
    if not name:
        raise ValueError("Nama wajib diisi")
    with db.transaction() as conn:
        row = conn.execute(f"SELECT id, name FROM {t} WHERE lower(btrim(name)) = lower(%s)", (name,)).fetchone()
        if row:
            conn.execute(f"UPDATE {t} SET is_active = true WHERE id = %s", (row["id"],))
            return {"id": str(row["id"]), "name": row["name"], "created": False}
        row = conn.execute(f"INSERT INTO {t} (name, created_by) VALUES (%s, %s) RETURNING id, name", (name, actor_id)).fetchone()
    return {"id": str(row["id"]), "name": row["name"], "created": True}


def update_master(kind: str, master_id: str, name: Optional[str], is_active: Optional[bool]) -> Optional[dict]:
    """Rename (accounts that carry the old name follow) or (de)activate a name."""
    t = MASTERS[kind]
    col = "job_title" if kind == "jobTitle" else "department"
    with db.transaction() as conn:
        row = conn.execute(f"SELECT id, name FROM {t} WHERE id = %s", (master_id,)).fetchone()
        if not row:
            return None
        if name is not None:
            name = " ".join(name.split())
            if not name:
                raise ValueError("Nama wajib diisi")
            clash = conn.execute(f"SELECT 1 FROM {t} WHERE lower(btrim(name)) = lower(%s) AND id <> %s",
                                 (name, master_id)).fetchone()
            if clash:
                raise ValueError(f"'{name}' sudah ada")
            conn.execute(f"UPDATE {t} SET name = %s WHERE id = %s", (name, master_id))
            conn.execute(f"UPDATE integration_portal.user_account SET {col} = %s WHERE lower(btrim({col})) = lower(btrim(%s))",
                         (name, row["name"]))
        if is_active is not None:
            conn.execute(f"UPDATE {t} SET is_active = %s WHERE id = %s", (is_active, master_id))
        out = conn.execute(f"SELECT id, name, is_active FROM {t} WHERE id = %s", (master_id,)).fetchone()
    return {"id": str(out["id"]), "name": out["name"], "isActive": out["is_active"]}
