"""Branch scope of the signed-in user.

Role "user" may only see the branches assigned to the account (integration_portal.user_branch);
superadmins see everything. A middleware in app/main.py sets `branch_scope` for every /api/
request from the session (None = unrestricted), and every data endpoint passes its `branch`
filter through `scoped_branch()`:

  - no branch requested  -> all branches of the user
  - branches requested   -> only those the user may see
  - nothing left         -> NO_BRANCH, a code that matches no row (empty result, never "all")
"""
from contextvars import ContextVar
from typing import Optional

from app.utils import normalize_branch, parse_branches

# None = unrestricted (superadmin / no session); a tuple = the allowed branch codes
branch_scope: ContextVar[Optional[tuple[str, ...]]] = ContextVar("branch_scope", default=None)

NO_BRANCH = "-"


def allowed_branches() -> Optional[tuple[str, ...]]:
    return branch_scope.get()


def scoped_branch(requested: Optional[str]) -> Optional[str]:
    """The branch filter to apply (comma separated codes), restricted to the user's branches."""
    allowed = branch_scope.get()
    if allowed is None:
        return normalize_branch(requested)
    wanted = parse_branches(requested)
    codes = [c for c in wanted if c in allowed] if wanted else sorted(allowed)
    return ",".join(sorted(codes)) if codes else NO_BRANCH


def in_scope(branch_code: Optional[str]) -> bool:
    allowed = branch_scope.get()
    return allowed is None or (branch_code or "") in allowed
