import copy
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import access, form_items, form_scope, travel_path as tp, visit_forms as vf
from app.routes import visit_forms as vf_routes

WIB, WITA = ZoneInfo("Asia/Jakarta"), ZoneInfo("Asia/Makassar")
CFG = {**tp.DEFAULTS, "since": "2026-11-26"}
D = date(2026, 11, 26)


def at(y, m, d, h, mi=0, zone=WIB):
    return datetime(y, m, d, h, mi, tzinfo=zone)


def test_the_sop_is_the_base_and_the_editors_know_the_form():
    items = [it for g in vf.DEFINITIONS["travel"]["groups"] for s in g["sections"] for it in s["items"]]
    assert len(items) == 24 and sum(i["category"] == "Kritikal" for i in items) == 10
    assert len({i["key"] for i in items}) == 24 and all(i["detail"] and i["standard"] for i in items)
    assert "travel" in form_items.FORMS and "travel" in form_scope.FORMS
    assert "travel" not in vf_routes.FORMS  # its own routes, never the generic visits
    assert "form.travel" in access.DEFAULT_ROLE_PAGES["outlet"] and {"form.travel", "mon.travel"} <= set(access.PAGE_KEYS)


def test_the_business_day_and_the_closing_window_cross_midnight():
    closing = CFG["sessions"][2]
    assert tp.business_day(at(2026, 11, 27, 1, 30), WIB, "04:00") == D
    assert tp.business_day(at(2026, 11, 27, 4, 0), WIB, "04:00") == date(2026, 11, 27)
    assert tp.window(closing, D, WIB, "04:00") == (at(2026, 11, 26, 20), at(2026, 11, 27, 2))
    # Bali: the same clock time is an hour earlier
    assert tp.window(CFG["sessions"][0], D, WITA, "04:00")[0] == at(2026, 11, 26, 5)


def test_sessions_follow_the_group():
    cfg = copy.deepcopy(CFG)
    cfg["sessions"][1]["groups"] = ["CCI"]
    cfg["sessions"][2]["active"] = False
    assert [s["key"] for s in tp.sessions_of(cfg, "CTG")] == ["opening"]
    assert [s["key"] for s in tp.sessions_of(cfg, "CCI")] == ["opening", "afternoon"]
    assert [s["key"] for s in tp.sessions_of(cfg, None)] == ["opening"]


ITEMS = [{"key": "a", "label": "A", "category": "Kritikal"}, {"key": "b", "label": "B", "category": "Mayor"}, {"key": "c", "label": "C", "category": "Minor"},
         {"key": "d", "label": "D", "category": "Minor", "required": False}]


def test_score_leaves_na_out_and_a_critical_finding_caps_it():
    ok = {"rating": "OK"}
    assert tp.score_of(ITEMS, {"a": ok, "b": ok, "c": {"rating": "NA"}}, CFG) | {"total": 4} == {
        "ok": 2, "nok": 0, "na": 1, "critical": 0, "done": 3, "total": 4, "score": 100.0, "band": "standard"}
    assert tp.score_of(ITEMS, {"a": ok, "b": ok, "c": {"rating": "NOK"}}, CFG)["score"] == 66.7
    many = [{"key": str(i), "label": "x", "category": "Minor"} for i in range(19)] + [ITEMS[0]]
    r = tp.score_of(many, {**{str(i): ok for i in range(19)}, "a": {"rating": "NOK"}}, CFG)
    assert r["score"] == 70 and r["band"] == "failed" and r["critical"] == 1  # 95% without the cap
    assert tp.score_of(many, {**{str(i): ok for i in range(19)}, "a": ok, "0": {"rating": "NOK"}}, CFG)["band"] == "standard"
    assert tp.score_of(ITEMS, {"a": {"rating": "NA"}}, CFG)["score"] is None


def test_what_blocks_the_sending():
    main = [{"item": k, "kind": "MAIN"} for k in "ab"]
    full = {"a": {"rating": "OK"}, "b": {"rating": "NOK", "note": "kotor"}, "c": {"rating": "NA", "note": "tidak ada"}}
    assert tp.gaps(ITEMS, full, main) == []  # d is optional
    assert [g["need"] for g in tp.gaps(ITEMS, {}, [])] == [["rating"]] * 3
    assert {g["key"]: g["need"] for g in tp.gaps(ITEMS, {"a": {"rating": "OK"}, "b": {"rating": "NOK"}, "c": {"rating": "NA"}}, [{"item": "a", "kind": "AFTER"}])} == {
        "a": ["photo"], "b": ["note", "photo"], "c": ["note"]}
    assert tp.gaps([{**ITEMS[0], "photo": "optional"}], {"a": {"rating": "OK"}}, []) == []
    assert tp.gaps([{**ITEMS[0], "photo": "none"}], {"a": {"rating": "NOK", "note": "rusak"}}, [])[0]["need"] == ["photo"]  # a finding always has a photo


def test_deadlines_per_category():
    t = at(2026, 11, 26, 9)
    nexts = [at(2026, 11, 26, 6), at(2026, 11, 26, 15), at(2026, 11, 26, 20)]
    assert tp.due_at(CFG, "Kritikal", t, nexts) == at(2026, 11, 26, 17)
    assert tp.due_at(CFG, "Mayor", t, nexts) == at(2026, 11, 27, 9)
    assert tp.due_at(CFG, "Minor", t, nexts) == at(2026, 11, 26, 15)
    assert tp.due_at(CFG, "Minor", t, []) == at(2026, 11, 27, 9) == tp.due_at(CFG, "Lain", t, [])


def test_compliance_counts_only_what_is_due():
    outs = [{"code": "A", "name": "A", "group": "CTG"}]
    rep = {("A", D, "opening"): {"id": "1", "late": True, "score": 90, "counts": {"critical": 1}}}
    rows, tot = tp.compliance(outs, rep, [date(2026, 11, 25), D], CFG, at(2026, 11, 26, 18), {})
    assert [c["state"] for c in rows[0]["days"][0]["sessions"]] == ["none"] * 3  # before it starts
    assert [c["state"] for c in rows[0]["days"][1]["sessions"]] == ["late", "missing", "waiting"]
    assert (rows[0]["expected"], rows[0]["done"], rows[0]["missing"], rows[0]["rate"], rows[0]["score"]) == (2, 1, 1, 50.0, 90.0)
    assert tot == {"expected": 2, "done": 1, "late": 1, "missing": 1, "rate": 50.0}


def _cfg(**kw):
    return {**copy.deepcopy(CFG), **kw}


def test_settings_are_checked():
    ok = tp.clean_settings(_cfg(sessions=[{"key": "opening", "label": " Buka ", "from": "7:00", "to": "10:00", "groups": ["CTG", "XX"]}]), {"CTG"})
    assert ok["sessions"][0] == {"key": "opening", "label": "Buka", "labelEn": "Buka", "from": "07:00", "to": "10:00", "active": True, "groups": ["CTG"]}
    for bad in ({"sessions": []}, {"dayEnds": "25:00"}, {"criticalCap": 120}, {"pass": 99}, {"since": "besok"}, {"due": {"Mayor": 0}}, {"reportMinutes": "x"},
                {"sessions": [CFG["sessions"][0], CFG["sessions"][0]]}, {"sessions": [{**CFG["sessions"][0], "to": "06:00"}]}):
        with pytest.raises(tp.TravelError):
            tp.clean_settings(_cfg(**bad), set())


def test_the_message_for_the_group_chat():
    r = {"submitted_at": at(2026, 11, 26, 10), "updated_at": None, "branch_name": "Joglo", "session_label": "Opening", "crew": "Ani", "supervisor": "Budi",
         "counts": {"done": 3, "total": 4, "score": 66.7},
         "answers": {"a": {"rating": "OK"}, "b": {"rating": "NOK", "note": "Sabun kosong", "fix": "refill 10.35"}, "c": {"rating": "NA", "note": "x"}}}
    t = tp.report_text(r, ITEMS, CFG)
    assert "TRAVEL PATH — Joglo" in t and "26/11/2026 · 10.00 WIB" in t and "Ani & Budi" in t and "3 / 4  ·  N/A: C" in t
    assert "1. [Mayor] B: Sabun kosong — diperbaiki: refill 10.35" in t and "@Head Operasional @Area Supervisor @Supervisor Outlet" in t
    assert "*Evaluasi*" in t and "Lampiran foto" not in t
    t = tp.report_text({**r, "share_token": "abc"}, ITEMS, CFG)
    assert t.endswith("@Supervisor Outlet") and "Lampiran foto: https://portal.kopicalf.co.id/share/travel/abc" in t


def test_the_automatic_evaluation():
    r = {"counts": {"score": 66.7}, "answers": {"a": {"rating": "NOK", "note": "x"}, "b": {"rating": "NOK", "note": "y", "fix": "ok"}}, "late": True, "outside_radius": True, "distance_m": 420}
    items = [{**ITEMS[0], "section_title": "Zona 1"}, {**ITEMS[1], "section_title": "Zona 1"}]
    e = tp.evaluation(r, items, CFG)
    assert "tidak lulus" in e[0] and "1 temuan Kritikal (A)" in e[1] and e[2] == "Temuan terbanyak: Zona 1 (2)."
    assert "1 dari 2 temuan langsung diperbaiki" in e[3] and "setelah jam sesi" in e[4] and "420 m" in e[5]
    assert "memenuhi standar" in tp.evaluation({"counts": {"score": 100}, "answers": {}}, ITEMS, CFG)[0]


def test_notices():
    f = {"id": "1", "branchCode": "A", "branchName": "Joglo", "label": "Chiller", "note": "7 C", "category": "Kritikal", "status": "OPEN", "overdue": False, "reopenedAt": None}
    people = {"A": {"outlet": ["o"], "pic": ["p"]}}
    assert [(n["user"], n["kind"]) for n in tp.notices([f], people)] == [("p", "travel_critical")]
    assert [(n["user"], n["link"]) for n in tp.notices([{**f, "overdue": True}], people)] == [("o", "/forms/travel"), ("p", None)]
    assert tp.notices([{**f, "category": "Minor"}], people) == []


def test_state_of_the_sessions(monkeypatch):
    monkeypatch.setattr(tp, "zone_of", lambda c: WIB)
    monkeypatch.setattr(tp.db, "fetch", lambda *a, **k: [])
    day, ss = tp.today_sessions({"code": "A", "group": None}, CFG, datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))  # 16:00 WIB
    assert day == D and [s["state"] for s in ss] == ["late", "open", "early"]
