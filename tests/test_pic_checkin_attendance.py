"""PIC Check In from the attendance punch-in (migration 121): the punch at an outlet becomes its check-in, once."""
from datetime import date, datetime

from app import pic_checkin as pc

USER = {"id": "u1", "username": "pic", "full_name": "PIC", "role": "user", "hrms_employee_id": "7", "branch_codes": ["A", "B"]}
OUTLETS = [{"code": "A", "name": "Outlet A", "latitude": -6.2, "longitude": 106.8, "radius_m": 50},
           {"code": "B", "name": "Outlet B", "latitude": -6.3, "longitude": 106.9, "radius_m": 50}]


def _fake(monkeypatch, punch, existing=None, form_done=False):
    calls = []

    def fetchrow(sql, params=()):
        calls.append(sql)
        if sql.strip().startswith("INSERT"):
            return {"id": "new", "visited_at": params["at"]}
        if "attendance_records" in sql:
            return punch
        if "WHERE attendance_id" in sql:
            return existing
        if "branch_code = %s" in sql and "visited_at AT TIME ZONE" in sql:
            return {"x": 1} if form_done else None
        if sql.strip().startswith("INSERT"):
            return {"id": "new", "visited_at": params["at"]}
        return None
    monkeypatch.setattr(pc.db, "fetchrow", fetchrow)
    monkeypatch.setattr(pc, "branch_options", lambda user, every=False: OUTLETS)
    monkeypatch.setattr(pc, "non_outlets", lambda codes: {c for c in codes if c.startswith("HUB")})
    return calls


def test_a_punch_in_at_an_outlet_becomes_its_check_in(monkeypatch):
    calls = _fake(monkeypatch, {"id": "a11", "check_in": datetime(2026, 11, 19, 8, 5), "latitude": -6.20005, "longitude": 106.80003})
    out = pc.from_attendance(USER, date(2026, 11, 19))
    assert out["branchCode"] == "A" and out["id"] == "new" and out["visitedAt"].startswith("2026-11-19T08:05")
    assert any(c.strip().startswith("INSERT") for c in calls)


def test_no_check_in_outside_every_radius_or_twice(monkeypatch):
    _fake(monkeypatch, {"id": "a11", "check_in": datetime(2026, 11, 19, 8, 5), "latitude": -6.25, "longitude": 106.85})
    assert pc.from_attendance(USER, date(2026, 11, 19)) is None  # at no outlet
    _fake(monkeypatch, {"id": "a11", "check_in": datetime(2026, 11, 19, 8, 5), "latitude": -6.2, "longitude": 106.8}, form_done=True)
    assert pc.from_attendance(USER, date(2026, 11, 19)) is None  # already checked in there through the form
    _fake(monkeypatch, {"id": "a11", "check_in": datetime(2026, 11, 19, 8, 5), "latitude": -6.2, "longitude": 106.8},
          existing={"id": "old", "branch_code": "A", "branch_name": "Outlet A", "visited_at": datetime(2026, 11, 19, 8, 5)})
    assert pc.from_attendance(USER, date(2026, 11, 19))["id"] == "old"  # one per punch


def test_no_link_or_no_punch(monkeypatch):
    _fake(monkeypatch, None)
    assert pc.from_attendance({**USER, "hrms_employee_id": None}) is None
    assert pc.from_attendance(USER, date(2026, 11, 19)) is None


def test_a_hub_inside_the_outlet_never_takes_the_punch(monkeypatch):
    hub = {"code": "HUB-A", "name": "Hub A", "latitude": -6.20001, "longitude": 106.80001, "radius_m": 50}
    _fake(monkeypatch, {"id": "a12", "check_in": datetime(2026, 11, 19, 8, 5), "latitude": -6.20001, "longitude": 106.80001})
    monkeypatch.setattr(pc, "branch_options", lambda user, every=False: [hub, *OUTLETS])
    assert pc.from_attendance(USER, date(2026, 11, 19))["branchCode"] == "A"  # the hub is closer, the outlet takes it
