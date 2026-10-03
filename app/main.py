"""
Integrated Portal Backend API (FastAPI)

Run locally: uvicorn app.main:app --reload --port 8002
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.utils import data_version
from app.routes import (branches_router, exports_router, live_router, overview_router, realtime_router, summary_router,
                        transactions_router)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("integrated_portal_be")

VERSION = "1.5.0"


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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def remember_data_version(request: Request, call_next):
    token = data_version.set(request.query_params.get("v", "")[:80])
    try:
        return await call_next(request)
    finally:
        data_version.reset(token)


app.include_router(transactions_router)
app.include_router(summary_router)
app.include_router(branches_router)
app.include_router(exports_router)
app.include_router(overview_router)
app.include_router(live_router)
app.include_router(realtime_router)


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
