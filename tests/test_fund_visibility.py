"""Who sees a Fund Request still in approval (app/pengajuan.py sees, 2026-11-27)."""
from app import pengajuan as pg

STEPS = [{"type": "APPROVE", "label": "Management", "users": [{"id": "m1", "name": "M"}]},
         {"type": "APPROVE", "label": "Head", "users": [{"id": "h1", "name": "H"}]},
         {"type": "PAY", "label": "Finance", "users": [{"id": "f1", "name": "F"}]}]


def row(status: str, step: int = 1) -> dict:
    return {"status": status, "approval_step": step}


def test_first_approver_sees_first():
    assert pg.sees({"id": "m1"}, row("SUBMITTED", 1), STEPS)
    assert not pg.sees({"id": "h1"}, row("SUBMITTED", 1), STEPS)
    assert not pg.sees({"id": "f1"}, row("SUBMITTED", 1), STEPS)


def test_next_approver_and_the_ones_before():
    assert pg.sees({"id": "h1"}, row("SUBMITTED", 2), STEPS)
    assert pg.sees({"id": "m1"}, row("SUBMITTED", 2), STEPS)
    assert not pg.sees({"id": "f1"}, row("REVISION", 2), STEPS)


def test_finance_after_every_approval():
    for s in ("APPROVED", "PAID", "VERIFYING", "DONE", "REJECTED"):
        assert pg.sees({"id": "f1"}, row(s, 2), STEPS)
    assert pg.sees({"id": "x9", "role": "superadmin"}, row("SUBMITTED", 1), STEPS)
    assert not pg.sees({"id": "x9", "role": "user"}, row("SUBMITTED", 1), STEPS)
