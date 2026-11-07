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


def test_cost_control_adjusts_the_quantities_with_a_note(monkeypatch):
    monkeypatch.setattr(st, "is_approver", lambda user: True)
    monkeypatch.setattr(st, "_load", lambda tid: {"id": tid, "number": "TS-1", "status": "accepted", "to_branch": A, "from_branch": B, "requested_by": "u"})
    monkeypatch.setattr(st.db, "fetch", lambda q, p=None: [{"item_code": "RM-MILK", "item_name": "Fresh Milk", "qty": 500}])
    user = {"id": "cc", "username": "cc"}
    with pytest.raises(st.TransferError, match="alasan penyesuaian"):
        st.decide(user, "t1", True, None, {"RM-MILK": 300})
    with pytest.raises(st.TransferError, match="lebih dari 0"):
        st.decide(user, "t1", True, "kurangi", {"RM-MILK": 0})
    with pytest.raises(st.TransferError, match="tidak ada di transfer"):
        st.decide(user, "t1", True, "kurangi", {"RM-SUGAR": 1})


def test_an_approved_transfer_leaves_the_source_and_reaches_the_requester_only_when_received(source):
    """2026-11-07: out of the source from the Cost Control approval, in the requester only once received."""
    at = datetime(2026, 11, 3, tzinfo=timezone.utc)
    source.append({"id": "t3", "status": "approved", "to_branch": B, "from_branch": B, "received_at": None, "cc_at": at,
                   "item_code": "RM-SUGAR", "unit": "GR", "qty": 50})
    sugar = st.stock([B])[B]["items"]["RM-SUGAR"]
    assert (sugar["out"], sugar["in"], sugar["pending"]) == (50.0, 0.0, 0.0)


def test_the_gr_of_a_received_transfer_is_matched_with_esb(monkeypatch):
    """2026-11-07 (migration 079): waiting until ESB syncs it, then matched or the differences per material."""
    items = [{"code": "RM-MILK", "name": "Fresh Milk", "qty": 500.0}]
    monkeypatch.setattr(st.db, "fetchrow", lambda q, p=None: None)
    assert st.gr_check("GR-1", A, items)["status"] == "waiting"
    monkeypatch.setattr(st.db, "fetchrow", lambda q, p=None: {"doc_num": "GR-1", "doc_date": None, "branch_code": A, "status_name": "Finished"})
    monkeypatch.setattr(st.db, "fetch", lambda q, p=None: [{"product_code": "RM-MILK", "qty": 500}])
    assert st.gr_check("GR-1", A, items)["status"] == "matched"
    assert st.gr_check("GR-1", B, items)["status"] == "otherBranch"
    monkeypatch.setattr(st.db, "fetch", lambda q, p=None: [{"product_code": "RM-MILK", "qty": 450}])
    assert st.gr_check("GR-1", A, items)["diffs"] == [{"code": "RM-MILK", "name": "Fresh Milk", "transfer": 500.0, "gr": 450.0}]
