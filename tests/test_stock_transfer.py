"""Transfer Stock (TITO, app/stock_transfer.py, migration 075): the stock available at the source, the rules of a request."""
from datetime import datetime, timezone

import pytest

from app import menu_costing as mc
from app import stock_transfer as st

A, B = "CCI01", "CCI02"


@pytest.fixture
def source(monkeypatch):
    """B counted on 1 Nov: 1000 GR of milk, 0 of sugar; 300 GR milk already asked by another request, 200 received after."""
    monkeypatch.setattr(mc, "portal_counts", lambda day, branches, back=1, ahead=1: {
        B: {"source": "portal", "date": "2026-11-01", "refs": ["SC-1"], "items": {"RM-MILK": (1000.0, "GR"), "RM-SUGAR": (0.0, "GR")}, "esb": {}}}
        if branches is None or B in branches else {})
    rows = [{"id": "t1", "status": "accepted", "to_branch": "X", "from_branch": B, "received_at": None, "item_code": "RM-MILK", "unit": "GR", "qty": 300},
            {"id": "t2", "status": "received", "to_branch": B, "from_branch": "Y", "received_at": datetime(2026, 11, 2, tzinfo=timezone.utc),
             "item_code": "RM-MILK", "unit": "GR", "qty": 200}]
    monkeypatch.setattr(st.db, "fetch", lambda q, p=None: rows if "stock_transfer t" in q else [])
    return rows


def test_available_is_the_count_plus_in_less_out_and_pending(source):
    milk = st.stock([B])[B]["items"]["RM-MILK"]
    assert (milk["so"], milk["in"], milk["pending"], milk["available"]) == (1000.0, 200.0, 300.0, 900.0)
    assert st.stock([B], exclude="t1")[B]["items"]["RM-MILK"]["available"] == 1200.0  # its own request left out


def _request(monkeypatch, lines, photo=True):
    monkeypatch.setattr(st, "branch_options", lambda user, every=False: [{"code": A, "name": "Outlet A"}] if not every
                        else [{"code": A, "name": "Outlet A"}, {"code": B, "name": "Outlet B"}])
    monkeypatch.setattr(st, "items_info", lambda codes: {})
    body = {"toBranch": A, "fromBranch": B, "lines": lines, "photo": "x" if photo else None, "signature": "y" if photo else None}
    return st.create({"id": "u1", "username": "pic"}, body)


def test_a_request_takes_at_most_what_the_source_has(source, monkeypatch):
    with pytest.raises(st.TransferError, match="kosong atau minus"):
        _request(monkeypatch, [{"code": "RM-SUGAR", "qty": 1}])
    with pytest.raises(st.TransferError, match="melebihi stok"):
        _request(monkeypatch, [{"code": "RM-MILK", "qty": 901}])
    with pytest.raises(st.TransferError, match="foto evidence dan tanda tangan"):
        _request(monkeypatch, [{"code": "RM-MILK", "qty": 100}], photo=False)


def test_only_my_outlet_asks_and_never_itself(source, monkeypatch):
    monkeypatch.setattr(st, "branch_options", lambda user, every=False: [{"code": A, "name": "Outlet A"}])
    with pytest.raises(st.TransferError, match="bukan outlet akun Anda"):
        st.create({"id": "u1", "username": "pic"}, {"toBranch": B, "fromBranch": A, "lines": [{"code": "RM-MILK", "qty": 1}]})
    with pytest.raises(st.TransferError, match="selain outlet Anda"):
        st.create({"id": "u1", "username": "pic"}, {"toBranch": A, "fromBranch": A, "lines": [{"code": "RM-MILK", "qty": 1}]})
