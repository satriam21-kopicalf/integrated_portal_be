from datetime import date, time

from app.aggregator_reports import proven
from app.alerts import fund_proofs
from app.pengajuan import CANCELLABLE, allowed_actions


def test_overdue_proof_goes_to_owner_and_payer():
    r = {"id": "x", "number": "PGJ-1", "paid_amount": 1000, "kind": "PERSONAL", "proof_due_on": date(2026, 11, 20),
         "user_id": "u1", "branch_code": None}
    out = fund_proofs([r], {"x": ["f1", "f2"]}, date(2026, 11, 24))
    assert [a["user"] for a in out] == ["u1", "f1", "f2"]
    assert out[0]["severity"] == "warn" and "4 hari" in out[0]["title"]
    assert fund_proofs([r], {}, date(2026, 11, 27))[0]["dedup"] != out[0]["dedup"]  # reminded every 3 days


def test_cancel_only_before_the_money():
    assert "PAID" not in CANCELLABLE and "VERIFYING" not in CANCELLABLE
    assert "CANCELLED" in allowed_actions("APPROVED") and "CANCELLED" not in allowed_actions("PAID")


def test_first_order_proves_open():
    row = {"status": "late", "openedAt": "09:00", "lateMin": 120}
    assert proven(row, time(7), 7 * 60 + 5, 15)["status"] == "on_time"
    later = proven(row, time(7), 8 * 60, 15)
    assert (later["status"], later["lateMin"], later["evidence"]) == ("late", 60, "order")
    assert proven(row, time(7), 10 * 60, 15) == row and proven(row, time(7), None, 15) == row
