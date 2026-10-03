"""Dashboard WebSocket: tells connected browsers when new data is available.

    wss://api.kopicalf.co.id/ws          (Cloudflare -> Traefik -> this service)
    GET /api/realtime/version            (HTTP fallback, same payload)

The server does not stream data itself; it watches two version stamps and
pushes an "update" message when one changes, so each page re-fetches what it
shows (with its own filters) through the normal REST endpoints:

    salesSyncedAt          newest synced_at of today's/yesterday's POS sales
                           (the ESB engine syncs hourly at :05, 7 days nightly)
    aggregatesRefreshedAt  newest refresh of the Overview aggregates (:20, 02:50)

Messages (JSON): {"type": "hello" | "update", "version", "salesSyncedAt",
"aggregatesRefreshedAt", "changed": [...]} and {"type": "ping"} every 25 s
(keeps Cloudflare/Traefik from closing an idle connection). Each uvicorn worker
runs its own watcher, only while it has connected clients.
"""
import asyncio
import fnmatch
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import database as db
from app.config import get_settings
from app.database import TABLE_TRANSACTIONS
from app.utils import TTLCache, today

logger = logging.getLogger("realtime")
router = APIRouter(tags=["realtime"])

POLL_SECONDS = 15
PING_SECONDS = 25
STAMPS = ("salesSyncedAt", "aggregatesRefreshedAt")

_cache = TTLCache()


def snapshot() -> dict:
    """Current version stamps (cheap: two indexed max() queries)."""
    row = db.fetchrow(
        f"""SELECT (SELECT max(synced_at) FROM {TABLE_TRANSACTIONS} WHERE sales_date >= %(since)s) AS sales,
                   (SELECT max(refreshed_at) FROM integration_portal.agg_refresh_log) AS aggregates""",
        {"since": (today() - timedelta(days=1)).isoformat()},
    ) or {}

    def iso(v):
        return v.isoformat() if v is not None else None

    state = {"salesSyncedAt": iso(row.get("sales")), "aggregatesRefreshedAt": iso(row.get("aggregates"))}
    state["version"] = "|".join(str(state[k]) for k in STAMPS)
    return state


def origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return False
    patterns = [p.strip() for p in get_settings().ws_allowed_origins.split(",") if p.strip()]
    return any(p == "*" or fnmatch.fnmatch(origin, p) for p in patterns)


class Hub:
    """Connected sockets of this worker plus the watcher that polls the stamps."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.state: Optional[dict] = None
        self.task: Optional[asyncio.Task] = None

    async def current(self) -> dict:
        if self.state is None:
            self.state = await run_in_threadpool(snapshot)
        return self.state

    async def join(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        await ws.send_json({"type": "hello", **(await self.current())})
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.watch())

    def leave(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 - a closed socket just leaves
                self.leave(ws)

    async def watch(self) -> None:
        while self.clients:
            await asyncio.sleep(POLL_SECONDS)
            try:
                new = await run_in_threadpool(snapshot)
            except Exception:  # noqa: BLE001 - keep watching through DB hiccups
                logger.exception("realtime snapshot failed")
                continue
            old = self.state or {}
            changed = [k for k in STAMPS if new.get(k) != old.get(k)]
            self.state = new
            if changed:
                await self.broadcast({"type": "update", "changed": changed, **new})


hub = Hub()


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    if not origin_allowed(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await hub.join(ws)
        while True:
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=PING_SECONDS)
                if message == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.leave(ws)


@router.get("/api/realtime/version")
def version():
    """Same stamps over HTTP, for clients whose WebSocket is unavailable."""
    state = _cache.get("version")
    if state is None:
        state = snapshot()
        _cache.set("version", state, 10)
    return JSONResponse(state)
