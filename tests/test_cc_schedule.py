"""Schedule of the Cost Control team (app/cc_schedule.py, migration 108)."""
from datetime import date, datetime, timezone

import pytest

from app import cc_schedule as cs


def test_the_plan_against_the_clock_ins(monkeypatch):
    monkeypatch.setattr(cs, "team", lambda: [{"id": "u1", "username": "rina", "name": "Rina"}])
    import app.utils as u
    monkeypatch.setattr(u, "today", lambda: date(2026, 11, 13))
    import app.pic_checkin as pc
    monkeypatch.setattr(pc, "_names", lambda codes: {c: f"Outlet {c}" for c in codes})
    plan = [{"id": 1, "user_id": "u1", "day": date(2026, 11, 12), "branch_code": "A", "note": None},
            {"id": 2, "user_id": "u1", "day": date(2026, 11, 12), "branch_code": "B", "note": None},
            {"id": 3, "user_id": "u1", "day": date(2026, 11, 14), "branch_code": "C", "note": "audit"}]
    visits = [{"user_id": "u1", "day": date(2026, 11, 12), "branch_code": "A", "branch_name": "Outlet A", "first_in": datetime(2026, 11, 12, 2, tzinfo=timezone.utc)},
              {"user_id": "u1", "day": date(2026, 11, 13), "branch_code": "D", "branch_name": "Outlet D", "first_in": datetime(2026, 11, 13, 3, tzinfo=timezone.utc)}]
    monkeypatch.setattr(cs.db, "fetch", lambda q, p=None: plan if "cc_visit_schedule" in q else visits)
    out = cs.week(date(2026, 11, 10), date(2026, 11, 16))
    st = {r["branchCode"]: r["status"] for r in out["data"]}
    assert st == {"A": "done", "B": "missed", "C": "planned"}
    assert [x["branchCode"] for x in out["unplanned"]] == ["D"]
    assert out["counts"] == {"done": 1, "missed": 1, "planned": 1, "unplanned": 1}
    with pytest.raises(cs.ScheduleError):
        cs.week(date(2026, 11, 10), date(2027, 2, 10))


def test_only_members_and_not_the_past(monkeypatch):
    monkeypatch.setattr(cs, "team", lambda: [{"id": "u1", "username": "rina", "name": "Rina"}])
    import app.utils as u
    monkeypatch.setattr(u, "today", lambda: date(2026, 11, 13))
    done = []

    class Conn:
        def execute(self, q, p=None):
            done.append((q.split()[0], p))
            return type("R", (), {"rowcount": 0})()

    class Tx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *x):
            return False
    monkeypatch.setattr(cs.db, "transaction", lambda: Tx())
    with pytest.raises(cs.ScheduleError):
        cs.set_day("nope", date(2026, 11, 14), ["A"], None, "lead")
    with pytest.raises(cs.ScheduleError):
        cs.set_day("u1", date(2026, 11, 10), ["A"], None, "lead")
    out = cs.set_day("u1", date(2026, 11, 14), ["B", "A", "A", " "], "  rute selatan ", "lead")
    assert out["branches"] == ["A", "B"] and [d[0] for d in done] == ["DELETE", "INSERT", "INSERT"]
    assert done[1][1][3] == "rute selatan"
