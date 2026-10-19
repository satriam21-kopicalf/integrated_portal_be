"""PIC Check In (app/pic_checkin.py, app/routes/pic_checkin.py): location rules, photo checks, face required, access."""
import base64
from datetime import datetime, timezone

import pytest

from app import pic_checkin
from tests.test_auth import anon, login, store  # noqa: F401 (fixtures)

KEMANG = {"code": "CCI01", "name": "Kopi Calf Kemang", "latitude": -6.2607, "longitude": 106.8137, "radius_m": 100}


def _jpeg() -> str:
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.full((240, 320, 3), 200, dtype=np.uint8))
    assert ok
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def test_location_rules():
    near = pic_checkin.judge(-6.2608, 106.8138, 12, KEMANG)
    assert near["distance_m"] < 20 and not near["outside_radius"] and not near["low_accuracy"]
    far = pic_checkin.judge(-6.2580, 106.8137, 10, KEMANG)  # ~300 m north
    assert 280 <= far["distance_m"] <= 320 and far["outside_radius"]
    weak = pic_checkin.judge(-6.2589, 106.8137, 150, KEMANG)  # ~200 m, ±150: 200 - 100 <= 100 → not outside, low accuracy
    assert not weak["outside_radius"] and weak["low_accuracy"]
    blind = pic_checkin.judge(-6.2580, 106.8137, 10, {**KEMANG, "latitude": None, "longitude": None})
    assert blind == {"distance_m": None, "outside_radius": False, "low_accuracy": False}


def test_photo_must_be_an_image_and_show_a_face():
    with pytest.raises(pic_checkin.CheckInError):
        pic_checkin.parse_photo("https://example.com/me.jpg")
    with pytest.raises(pic_checkin.CheckInError):  # declared PNG, bytes are not
        pic_checkin.parse_photo("data:image/png;base64," + base64.b64encode(b"not a png at all").decode())
    assert pic_checkin.face_check(_jpeg()) == 0  # a plain grey picture: no face


def test_check_in_flow_and_access(anon, store, fake_db, page_access, monkeypatch):
    monkeypatch.setattr(pic_checkin, "branch_options", lambda user: [KEMANG])
    monkeypatch.setattr(pic_checkin, "recent_of", lambda uid, limit=10: [])
    saved = {}

    def insert(query, params=None):
        saved.update(params)
        return {"id": "11111111-1111-1111-1111-111111111111", "visited_at": datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)}

    monkeypatch.setattr(pic_checkin.db, "fetchrow", insert)
    body = {"branchCode": "CCI01", "photo": _jpeg(), "latitude": -6.2608, "longitude": 106.8138, "accuracyM": 12}

    login(anon, "kasir", "Kasir1234")  # role user: the form is on by default, monitoring is not
    form = anon.get("/api/forms/pic-check-in").json()
    assert form["user"]["username"] == "kasir" and form["branches"][0]["code"] == "CCI01"
    assert anon.get("/api/monitoring/pic-check-in").status_code == 403

    monkeypatch.setattr(pic_checkin, "count_faces", lambda data: 0)
    r = anon.post("/api/forms/pic-check-in", json=body)
    assert r.status_code == 422 and "Wajah tidak terdeteksi" in r.json()["error"] and not saved
    assert anon.post("/api/forms/pic-check-in", json={**body, "branchCode": "CTG54"}).status_code == 422
    assert anon.post("/api/forms/pic-check-in", json={k: v for k, v in body.items() if k != "latitude"}).status_code == 422

    monkeypatch.setattr(pic_checkin, "count_faces", lambda data: 1)
    assert anon.post("/api/forms/pic-check-in/face-check", json={"photo": body["photo"]}).json() == {"faces": 1}
    r = anon.post("/api/forms/pic-check-in", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["branchName"] == "Kopi Calf Kemang" and out["outsideRadius"] is False and out["distanceM"] < 20
    assert saved["un"] == "kasir" and saved["bc"] == "CCI01" and saved["faces"] == 1 and saved["photo"].startswith(b"\xff\xd8")
    assert any(e["action"] == "form.pic_check_in" and e["status"] == "ok" for e in fake_db.activity)

    page_access.overrides[str(store.user["id"])] = {"form.pic-check-in": False}
    assert anon.get("/api/forms/pic-check-in").status_code == 403
