"""WebSocket /ws: origin check, hello with the version stamps, updates when a stamp changes."""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.routes import realtime

STATE = {"salesSyncedAt": "2026-10-03T06:05:48+00:00", "aggregatesRefreshedAt": "2026-10-03T06:20:07+00:00"}


@pytest.fixture
def stamps(monkeypatch):
    state = dict(STATE)
    monkeypatch.setattr(realtime, "snapshot", lambda: {**state, "version": "|".join(state[k] for k in realtime.STAMPS)})
    realtime.hub.state = None
    realtime._cache._data.clear()
    return state


def test_hello_and_update(client, stamps, monkeypatch):
    monkeypatch.setattr(realtime, "POLL_SECONDS", 0.05)
    with client.websocket_connect("/ws", headers={"origin": "https://portal.kopicalf.co.id"}) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["salesSyncedAt"] == STATE["salesSyncedAt"]
        stamps["salesSyncedAt"] = "2026-10-03T07:05:41+00:00"
        update = ws.receive_json()
        assert update["type"] == "update" and update["changed"] == ["salesSyncedAt"]
        assert update["version"] != hello["version"]
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"


def test_unknown_origin_is_refused(client, stamps):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_origin_patterns():
    assert realtime.origin_allowed("https://portal.kopicalf.co.id")
    assert realtime.origin_allowed("https://integrated-portal-git-main-x.vercel.app")
    assert realtime.origin_allowed("http://localhost:3002")
    assert not realtime.origin_allowed(None) and not realtime.origin_allowed("https://kopicalf.co.id.evil.com")


def test_http_fallback(client, stamps):
    body = client.get("/api/realtime/version").json()
    assert body["aggregatesRefreshedAt"] == STATE["aggregatesRefreshedAt"] and "version" in body
