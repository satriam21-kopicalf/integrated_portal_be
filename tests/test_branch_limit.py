from app.accounts import branch_limit


def test_branch_limit():
    assert branch_limit({"role": "superadmin", "branch_codes": []}) is None
    assert branch_limit({"role": "admin", "branch_codes": ["A"]}) == ("A",)
    assert branch_limit({"role": "cost_control", "branch_codes": ["A"]}) is None
    assert branch_limit({"role": "user", "branch_codes": []}) == ()
