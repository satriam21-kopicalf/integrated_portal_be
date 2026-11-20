from decimal import Decimal
from unittest.mock import patch

import pytest

from app import pengajuan as pg


def test_settle_diff():
    assert pg.settle_diff(1_000_000, 1_250_000) == {"diff": 250000.0, "outcome": "PAY"}
    assert pg.settle_diff(1_000_000, 800_000) == {"diff": -200000.0, "outcome": "RETURN"}
    assert pg.settle_diff(500_000, 500_000)["outcome"] == "EVEN"


def test_flow_key_per_purpose():
    assert pg.flow_key({"kind": "PERSONAL", "purpose_id": 7}) == "PERSONAL:7"
    assert pg.flow_key({"kind": "PERSONAL", "purpose_id": None}) == "PERSONAL"
    assert pg.flow_key({"kind": "PETTY_CASH"}) == "PETTY_CASH"


def test_steps_fall_back_to_default_personal_flow():
    default = [{"id": 1, "position": 1, "label": "Approval Management", "type": "APPROVE", "users": []}]
    with patch.object(pg, "flow", side_effect=lambda k: [] if k == "PERSONAL:7" else default):
        assert pg.steps_of({"kind": "PERSONAL", "purpose_id": 7}) == default


def _row(**kw):
    base = {"purpose_id": 1, "transaction_type_id": 2, "needed_on": "2026-11-21", "purpose": "Tiket kereta", "requested_amount": Decimal(100),
            "branch_code": None, "account_type_id": 3, "payee_bank": "BCA", "payee_account": "123", "payee_name": "A", "settles_id": None}
    return {**base, **kw}


def _purpose(settings):
    return patch("app.fund_options.get", return_value={"id": 1, "list": "PURPOSE", "settings": settings})


def test_missing_personal_complete():
    with _purpose({}):
        assert pg.missing_personal(_row(), []) == []


def test_missing_personal_flags():
    with _purpose({"receipt": True, "outlet": True, "type": "SETTLEMENT"}):
        out = pg.missing_personal(_row(), [])
    assert "Pilih outlet" in out
    assert "Pilih Cash in Advance yang diselesaikan" in out
    assert any("dokumen pendukung" in m for m in out)


def test_dept_plafon_refuses_above():
    with _purpose({"deptPlafon": True}), patch("app.fund_options.dept_plafon", return_value=Decimal(50)):
        with pytest.raises(pg.PengajuanError):
            pg.check_personal(_row(department="Marketing"))
    with _purpose({"deptPlafon": True}), patch("app.fund_options.dept_plafon", return_value=None):
        assert pg.check_personal(_row(department="Marketing")) is None
