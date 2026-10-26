"""Company (brand) of the sales endpoints: Kopi Calf (CALF) and Wasgee Tea (CALF7) never mix (app/scope.py)."""

from app import scope


def test_company_of_request():
    assert scope.company_of_request("/api/overview/kpis", None) == "CALF"
    assert scope.company_of_request("/api/summary", "calf7") == "CALF7"
    assert scope.company_of_request("/api/branches", "all") is None
    # forms, stock opname, accounts: never narrowed to a company
    assert scope.company_of_request("/api/forms/pic-check-in", "CALF7") is None
    assert scope.company_of_request("/api/users", None) is None


def _with(branches, company, fn):
    b, c = scope.branch_scope.set(branches), scope.company_scope.set(company)
    try:
        return fn()
    finally:
        scope.branch_scope.reset(b)
        scope.company_scope.reset(c)


def test_scoped_branch_by_company(client):
    scope._cache._data.clear()
    # superadmin: every branch of the company; Kopi Calf with its closed outlets (fixture: every code but WSG02)
    assert _with(None, "CALF", lambda: scope.scoped_branch(None)) == "CCI01,CCI04,CTG54,OLD01,TGP17"
    assert _with(None, "CALF7", lambda: scope.scoped_branch(None)) == "WSG02"
    # a branch of the other company is never read
    assert _with(None, "CALF", lambda: scope.scoped_branch("WSG02")) == scope.NO_BRANCH
    assert _with(None, None, lambda: scope.scoped_branch("WSG02")) == "WSG02"
    # role user: its branches, of that company only
    assert _with(("CCI01", "WSG02"), "CALF7", lambda: scope.scoped_branch(None)) == "WSG02"
    assert _with(("CCI01",), "CALF7", lambda: scope.scoped_branch(None)) == scope.NO_BRANCH
    assert _with(("CCI01", "WSG02"), "CALF7", lambda: scope.in_scope("CCI01")) is False


def test_summary_reads_the_branches_of_the_company(client, fake_db):
    scope._cache._data.clear()
    client.get("/api/summary?dateFrom=2026-09-29&dateTo=2026-09-30&company=CALF7")
    params = [p for q, p in fake_db.queries if "branch_code = ANY" in q]
    assert params and ["WSG02"] in [x for p in params for x in (p if isinstance(p, (list, tuple)) else [p])]
