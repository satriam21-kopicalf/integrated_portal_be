"""Aggregator Insight PDF: the recommendations from the figures (app/aggregator_pdf.py recommend)."""
from datetime import date

from app import aggregator_pdf as pdf


def facts(**over):
    f = {"period": (date(2026, 11, 20), date(2026, 11, 26)), "platform": None, "hold": [], "stale": 0, "unmapped": 0, "topics": [],
         "opening": {"onTime": 90, "counted": 100, "late": 10, "notOpened": 0, "earlyClose": 0, "pauseMin": 0},
         "void": {"n": 0, "noReason": 0, "pct": 0, "amount": 0}, "reviews": {"low": 0, "noTicket": 0}, "complaints": {"open": 0, "progress": 0},
         "oos": {"outNow": 0, "outletsWithOos": 0}}
    f.update(over)
    return f


def test_nothing_to_do():
    assert pdf.recommend(facts()) == []


def test_hold_and_late_first():
    out = pdf.recommend(facts(hold=[{"branchName": "Koja", "name": "x", "platform": "GrabFood"}], stale=3,
                              opening={"onTime": 50, "counted": 100, "late": 50, "notOpened": 1, "earlyClose": 0, "pauseMin": 0}))
    assert [x[0] for x in out] == ["HIGH", "HIGH", "LOW"] and "Koja (GrabFood)" in out[0][2] and "50,0%" in out[1][1]


def test_void_without_reason():
    out = pdf.recommend(facts(void={"n": 10, "noReason": 10, "pct": 1.2, "amount": 100}))
    assert out[0][0] == "HIGH" and "100,0% void" in out[0][1]


def test_a_platform_rated_low():
    out = pdf.recommend(facts(byPlatform=[{"platform": "GoFood", "avg": 3.58, "n": 300}, {"platform": "GrabFood", "avg": 4.9, "n": 300}]))
    assert out[0][0] == "HIGH" and "GoFood 3,58" in out[0][1]
