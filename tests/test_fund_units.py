from datetime import date

from app.fund_units import unit_of
from app.pengajuan import flow_keys, missing_vendor


def test_unit_follows_the_place():
    types = {"HUB-WH-X": "HUB_WH", "HUB-CK-X": "HUB_CK", "ROASTERY": "ROASTERY", "WAREHOUSE-PUSAT": "WH_CENTRAL", "CTG01": "OUTLET"}
    got = {c: unit_of(c, types, ("WSG1",)) for c in (*types, "WSG1", None, "HOF")}
    assert got == {"HUB-WH-X": "WAREHOUSE", "HUB-CK-X": "CK", "ROASTERY": "ROASTERY", "WAREHOUSE-PUSAT": "WAREHOUSE",
                   "CTG01": "CALF", "WSG1": "CALF7", None: "CALF", "HOF": "CALF"}


def test_a_unit_flow_goes_before_the_kind():
    assert flow_keys({"kind": "VENDOR", "unit": "CK"}) == ["VENDOR@CK", "VENDOR"]
    assert flow_keys({"kind": "PERSONAL", "purpose_id": 3, "unit": "COTR"}) == ["PERSONAL:3@COTR", "PERSONAL:3", "PERSONAL@COTR", "PERSONAL"]


def test_vendor_payment_needs():
    row = {"vendor_name": "SUKANDA", "payment_term": "TOP", "invoice_no": "FP-1", "invoice_date": date(2026, 11, 2), "needed_on": date(2026, 11, 14),
           "purpose": "Fresh milk", "requested_amount": 100, "outstanding_amount": 100, "payee_type": "BANK", "payee_bank": "BCA",
           "payee_account": "146", "payee_name": "PT SUKANDA"}
    assert missing_vendor(row, [{"kind": "OTHER"}]) == []  # past due dates are allowed
    assert len(missing_vendor({**row, "requested_amount": 200, "vendor_name": ""}, [])) == 3
