"""Stock opname in the Cost Control menu (D-51): every call goes to the kopicalf-platform stock-count API.

    /api/so/{path}     -> {SO_GATEWAY_URL}/api/{path}   (any method; JSON, photos and Excel files pass through)
    GET /api/so-sites  (superadmin) stock-opname sites without an ESB branch (HUB WH, HUB CK, ...) for User Accounts

kopicalf-platform keeps the only implementation (lists, schedules, counting, photos, review, ESB export) in
warehouse-backend /portal. This route signs who is asking (X-Portal-Assertion, HMAC-SHA256 with SO_GATEWAY_SECRET):
user id, name, role and, for role "user", the kopicalf facilities of the user's branches. ESB branch codes are
matched through kc_master.facility_esb_map; HUB codes are kopicalf facility codes. kopicalf decides the rights:
"user" counts at those facilities, "cost_control" and "superadmin" monitor, review, change settings and export.
"""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import access, accounts
from app import database as db
from app.config import get_settings
from app.routes.auth import current_user, require_superadmin
from app.utils import TTLCache

logger = logging.getLogger("stock_opname")
router = APIRouter(tags=["stock opname"])
_facilities = TTLCache()
_http = requests.Session()
PASS_HEADERS = ("content-type", "content-disposition", "cache-control", "x-content-sha256")


def facilities_of(codes: list[str]) -> list[str]:
    """kopicalf facility ids of branch codes (ESB branch codes through the ESB mapping, or facility codes)."""
    if not codes:
        return []
    key = ",".join(sorted(codes))
    hit = _facilities.get(key)
    if hit is None:
        rows = db.fetch(
            """SELECT DISTINCT f.id::text AS id FROM kc_master.facility f
               WHERE f.is_active AND (f.code = ANY(%(c)s) OR EXISTS (
                   SELECT 1 FROM kc_master.facility_esb_map m
                   JOIN integration_esb.master_branches b ON b.company_code = m.company_code AND b.branch_id::text = m.esb_id
                   WHERE m.facility_id = f.id AND b.branch_code = ANY(%(c)s)))""",
            {"c": list(codes)},
        )
        hit = sorted(r["id"] for r in rows)
        _facilities.set(key, hit, 60)
    return hit


def assertion(user: dict, secret: str) -> str:
    role = access.so_role(user) if user["role"] in accounts.ROLES else "user"
    payload = {
        "sub": str(user["id"]),
        "username": user.get("username") or "",
        "name": user.get("full_name") or user.get("username") or "",
        "role": role,
        "facilities": [] if role in accounts.GLOBAL_ROLES else facilities_of(list(user.get("branch_codes") or [])),
        "exp": int(time.time()) + 60,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return body + "." + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@router.get("/api/so-sites")
def so_sites(_user: dict = Depends(require_superadmin)) -> list[dict]:
    """Sites to assign for stock opname that have no ESB branch code (HUB WH, HUB CK, warehouses, kitchens)."""
    rows = db.fetch(
        """SELECT f.code, f.name, f.facility_type FROM kc_master.facility f
           WHERE f.is_active AND f.facility_type NOT IN ('OUTLET', 'VIRTUAL', 'OFFICE')
           ORDER BY f.facility_type, f.name""")
    return [{"branch_code": r["code"], "branch_name": r["name"], "facility_type": r["facility_type"]} for r in rows]


@router.api_route("/api/so/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def so_proxy(path: str, request: Request, user: dict = Depends(current_user)) -> Response:
    s = get_settings()
    if not s.so_gateway_secret:
        raise HTTPException(503, detail="Stock opname belum diaktifkan")
    pages = access.so_page(request.method, path)
    if user["role"] != "superadmin" and not any(access.has_page(user, p) for p in pages):
        access.deny(request, user)
    body = await request.body()
    headers = {"x-portal-assertion": assertion(user, s.so_gateway_secret)}
    if request.headers.get("content-type"):
        headers["content-type"] = request.headers["content-type"]
    url = f"{s.so_gateway_url.rstrip('/')}/api/{path}"
    try:
        r = await _forward(request.method, url, request.url.query, body, headers, s.so_gateway_timeout)
    except requests.RequestException as exc:
        logger.warning("stock opname gateway %s %s failed: %s", request.method, path, exc)
        raise HTTPException(502, detail="Layanan stock opname tidak dapat dihubungi, coba lagi") from None
    out = {k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
    return Response(r.content, status_code=r.status_code, headers=out)


async def _forward(method: str, url: str, query: str, body: bytes, headers: dict, timeout: int) -> requests.Response:
    from starlette.concurrency import run_in_threadpool

    def call() -> requests.Response:
        return _http.request(method, url + (f"?{query}" if query else ""), data=body or None, headers=headers,
                             timeout=timeout)

    return await run_in_threadpool(call)

