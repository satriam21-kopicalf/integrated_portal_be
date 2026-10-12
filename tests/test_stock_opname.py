"""Stock opname gateway (D-51): signed identity, branches -> facilities, roles, pass-through of the response."""
import base64
import hashlib
import hmac
import json

import pytest
import requests

from app.config import get_settings
from app.routes import stock_opname
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

SECRET = "k" * 40


class FakeResponse:
    def __init__(self, status=200, content=b'{"data": []}', headers=None):
        self.status_code, self.content = status, content
        self.headers = headers or {"content-type": "application/json", "set-cookie": "x=1"}


@pytest.fixture
def gateway(monkeypatch):
    calls = []

    async def forward(method, url, query, body, headers, timeout):
        calls.append({"method": method, "url": url, "query": query, "body": body, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr(stock_opname, "_forward", forward)
    monkeypatch.setattr(stock_opname, "facilities_of", lambda codes: [f"fac-{c}" for c in sorted(codes)])
    monkeypatch.setattr(get_settings(), "so_gateway_secret", SECRET)
    return calls


def payload(headers):
    body, mac = headers["x-portal-assertion"].rsplit(".", 1)
    assert hmac.compare_digest(mac, hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest())
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_needs_sign_in_and_secret(anon, store, monkeypatch):
    assert anon.get("/api/so/counts/today").status_code == 401
    monkeypatch.setattr(get_settings(), "so_gateway_secret", "")
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/so/counts/today").status_code == 503


def test_pic_signed_with_branch_facilities(anon, store, gateway):
    login(anon, "kasir", "Kasir1234")  # role user, branch CCI01
    r = anon.post("/api/so/counts?x=1", json={"facility_id": "f1", "count_type": "DAILY_PACKAGING"})
    assert r.status_code == 200 and r.json() == {"data": []} and "set-cookie" not in r.headers
    call = gateway[-1]
    assert call["method"] == "POST" and call["url"].endswith("/portal/api/counts") and call["query"] == "x=1"
    p = payload(call["headers"])
    assert (p["role"], p["facilities"], p["username"]) == ("user", ["fac-CCI01"], "kasir")
    assert json.loads(call["body"]) == {"facility_id": "f1", "count_type": "DAILY_PACKAGING"}


def test_cost_control_role_is_global(anon, store, gateway):
    store.add("cc.spv", "cc@kopicalf.co.id", "Cost Control", "Control1234", role="cost_control")
    login(anon, "cc.spv", "Control1234")
    anon.get("/api/so/counts/monitor")
    p = payload(gateway[-1]["headers"])
    assert p["role"] == "cost_control" and p["facilities"] == []
    assert anon.get("/api/users").status_code == 403  # no user management
    assert anon.get("/api/cost-control/meta").status_code != 403  # Cost Control menu


def test_gateway_down_is_reported(anon, store, monkeypatch):
    async def down(*_a, **_k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(stock_opname, "_forward", down)
    monkeypatch.setattr(stock_opname, "facilities_of", lambda codes: [])
    monkeypatch.setattr(get_settings(), "so_gateway_secret", SECRET)
    login(anon, "kasir", "Kasir1234")
    assert anon.get("/api/so/counts/today").status_code == 502


def test_superadmin_creates_cost_control_without_branches(anon, store):
    login(anon, "superadmin", "Admin1234")
    r = anon.post("/api/users", json={"username": "cc.two", "email": "cc2@kopicalf.co.id", "password": "Control1234",
                                      "role": "cost_control", "branches": ["CCI01"]})
    user = r.json()["user"]
    assert r.status_code == 201 and user["role"] == "cost_control" and user["branches"] == []
