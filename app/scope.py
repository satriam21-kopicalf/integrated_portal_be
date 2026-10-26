"""Branch scope of the signed-in user, and the company (brand) a sales view is about.

Role "user" may only see the branches assigned to the account (integration_portal.user_branch);
superadmins see everything. A middleware in app/main.py sets `branch_scope` for every /api/
request from the session (None = unrestricted), and every data endpoint passes its `branch`
filter through `scoped_branch()`:

  - no branch requested  -> all branches of the user
  - branches requested   -> only those the user may see
  - nothing left         -> NO_BRANCH, a code that matches no row (empty result, never "all")

Company (2026-10-26): the POS data holds several ESB companies (CALF = the Kopi Calf outlets, CALF7 = Wasgee Tea;
integrated-esbapi ESB_POS_COMPANIES). The sales endpoints (Overview, Sales Transactions, Live, branches, exports;
`SALES_PATHS`) look at one company at a time so the brands never mix: `company_scope` holds the company of the request
(query parameter `company`, default the main company; "all" = every company) and narrows the branches above to the
branches of that company. Other endpoints (forms, stock opname, accounts) are not narrowed.
"""
from contextvars import ContextVar
from typing import Optional

from app import database as db
from app.utils import TTLCache, normalize_branch, parse_branches

# None = unrestricted (superadmin / no session); a tuple = the allowed branch codes
branch_scope: ContextVar[Optional[tuple[str, ...]]] = ContextVar("branch_scope", default=None)
# None = every company; a code = only the branches of that company
company_scope: ContextVar[Optional[str]] = ContextVar("company_scope", default=None)

NO_BRANCH = "-"
MAIN_COMPANY = "CALF"
# names of the companies (brands) in the filters and file names
COMPANY_NAMES = {"CALF": "Kopi Calf", "CALF7": "Wasgee Tea"}
ALL_COMPANIES = "all"
# endpoints whose figures are about one company at a time (prefixes of the request path)
SALES_PATHS = ("/api/overview", "/api/summary", "/api/transactions", "/api/live", "/api/branches", "/api/exports")

_cache = TTLCache()


def company_branches(company: str) -> tuple[str, ...]:
    """Branch codes of a company: its branches in the ESB branch master (the POS engine adds the outlets of the other
    companies); for the main company also every branch that ever sold (closed outlets no longer in the master) unless
    it belongs to another company."""
    hit = _cache.get(f"company:{company}")
    if hit is None:
        rows = db.fetch(
            """SELECT branch_code FROM integration_esb.master_branches
               WHERE COALESCE(branch_code, '') <> '' AND COALESCE(company_code, %(main)s) = %(c)s
               UNION
               SELECT DISTINCT d.branch_code FROM integration_portal.agg_sales_daily d
               WHERE %(c)s = %(main)s AND COALESCE(d.branch_code, '') <> '' AND NOT EXISTS (
                   SELECT 1 FROM integration_esb.master_branches b
                   WHERE b.branch_code = d.branch_code AND COALESCE(b.company_code, %(main)s) <> %(main)s)""",
            {"main": MAIN_COMPANY, "c": company})
        hit = tuple(sorted(r["branch_code"] for r in rows))
        _cache.set(f"company:{company}", hit, 300)
    return hit


def company_of_request(path: str, requested: Optional[str]) -> Optional[str]:
    """The company a request is about: None (not narrowed) outside the sales endpoints or for "all"."""
    if not path.startswith(SALES_PATHS):
        return None
    value = (requested or "").strip()
    if value.lower() == ALL_COMPANIES:
        return None
    return value.upper() or MAIN_COMPANY


def allowed_branches() -> Optional[tuple[str, ...]]:
    """The branches this request may read: the user's branches, narrowed to the company of the request."""
    allowed = branch_scope.get()
    company = company_scope.get()
    if company is None:
        return allowed
    codes = company_branches(company)
    if allowed is None:
        return codes
    mine = set(allowed)
    return tuple(c for c in codes if c in mine)


def scoped_branch(requested: Optional[str]) -> Optional[str]:
    """The branch filter to apply (comma separated codes), restricted to the user's branches (and the company)."""
    allowed = allowed_branches()
    if allowed is None:
        return normalize_branch(requested)
    wanted = parse_branches(requested)
    codes = [c for c in wanted if c in allowed] if wanted else sorted(allowed)
    return ",".join(sorted(codes)) if codes else NO_BRANCH


def is_whole_scope(branch: Optional[str]) -> bool:
    """True when `branch` (a scoped filter) is every branch this request may read, i.e. nothing was narrowed by hand."""
    allowed = allowed_branches()
    return branch is None or (allowed is not None and set(parse_branches(branch)) == set(allowed))


def in_scope(branch_code: Optional[str]) -> bool:
    allowed = allowed_branches()
    return allowed is None or (branch_code or "") in allowed
