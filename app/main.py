"""
Integrated Portal Backend API (FastAPI)

Run locally: uvicorn app.main:app --reload --port 8002
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import database as db
from app.config import get_settings
from app.utils import data_version
from app.routes.activity import router as activity_router
from app.routes import (auth_router, avatars_router, branches_router, cost_control_router, exports_router, live_router,
                        overview_router, realtime_router,
                        summary_router, transactions_router, users_router)
from app.routes.auth import current_user, require_superadmin, session_lookup
from app.scope import branch_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("integrated_portal_be")

VERSION = "1.6.0"


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZip JSON responses but not file downloads (xlsx is already compressed)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/download"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.open_pool()
    logger.info("Integrated Portal Backend started (port %s)", get_settings().port)
    yield
    db.close_pool()


app = FastAPI(
    title="Integrated Portal Backend API",
    description="Backend API for the Kopi Calf Integrated Portal (ESB POS transactions).",
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def scope_to_user_branches(request: Request, call_next):
    """Role "user": every data endpoint only sees the user's branches (app/scope.py)."""
    token = None
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/"):
        user = await run_in_threadpool(session_lookup, request)
        if user and user["role"] != "superadmin":
            token = branch_scope.set(tuple(user.get("branch_codes") or ()))
    try:
        return await call_next(request)
    finally:
        if token is not None:
            branch_scope.reset(token)


@app.middleware("http")
async def remember_data_version(request: Request, call_next):
    token = data_version.set(request.query_params.get("v", "")[:80])
    try:
        return await call_next(request)
    finally:
        data_version.reset(token)


# every data endpoint needs a signed-in user; /api/users additionally a superadmin.
# Open: /health, /, /api/auth/login|logout and the /ws socket (it only carries
# data-version stamps, no data; its HTTP twin /api/realtime/version is protected).
signed_in = [Depends(current_user)]
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(avatars_router, dependencies=signed_in)
app.include_router(transactions_router, dependencies=signed_in)
app.include_router(summary_router, dependencies=signed_in)
app.include_router(branches_router, dependencies=signed_in)
app.include_router(exports_router, dependencies=signed_in)
app.include_router(overview_router, dependencies=signed_in)
app.include_router(live_router, dependencies=signed_in)
app.include_router(cost_control_router, dependencies=[Depends(require_superadmin)])  # superadmin only
app.include_router(activity_router)  # superadmin reads; any signed-in user reports page views
app.include_router(realtime_router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    # same shape as the other API errors: {"error": "..."}
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "Internal server error", "details": str(exc)}, status_code=500)


@app.get("/", tags=["health"])
def root():
    return {"status": "ok", "service": "integrated_portal_be", "version": VERSION, "docs": "/docs"}


@app.get("/health", tags=["health"])
def health():
    try:
        db.fetch("SELECT 1")
        return {"status": "healthy", "database": "connected", "version": VERSION}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"status": "unhealthy", "database": f"error: {exc}", "version": VERSION}, status_code=503
        )
