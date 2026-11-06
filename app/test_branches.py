"""Test branches (2026-11-07, migration 078): outlets for the Cost Control team to test every menu and form without
touching the real figures.

A test branch is a kopicalf facility of its own (code DMY-…, no ESB branch) with an outlet account. What happens at it
— stock opname, waste, Transfer Stock — is stored under its own code; what ESB records (sales, menus, goods receipts and
the other documents) it reads from its source branch, without copying a row, so no total of the real branches changes.
Every calculation over all branches leaves the test branches out (app/menu_costing.py portal_counts); a test branch
transfers stock only with another test branch.
"""
from typing import Optional

from app import database as db
from app.utils import TTLCache

T = "integration_portal.test_branch"
_cache = TTLCache()


def sources() -> dict[str, str]:
    """{test branch code: source branch code} (cached 60 s)."""
    hit = _cache.get("all")
    if hit is None:
        try:
            hit = {r["code"]: r["source_branch"] for r in db.fetch(f"SELECT code, source_branch FROM {T}")}
        except Exception:  # noqa: BLE001 - migration 078 not applied yet: no test branch
            hit = {}
        _cache.set("all", hit, 60)
    return hit


def is_test(code: Optional[str]) -> bool:
    return bool(code) and code in sources()


def real(codes: Optional[list[str]]) -> Optional[list[str]]:
    """The codes whose ESB data is read: a test branch reads its source branch."""
    if codes is None:
        return None
    s = sources()
    return sorted({s.get(c, c) for c in codes})


def readers(codes: list[str]) -> dict[str, list[str]]:
    """{code whose ESB data is read: [branches that get it]}: a source branch gives its rows to its test branches too."""
    s = sources()
    out: dict[str, list[str]] = {}
    for c in codes:
        out.setdefault(s.get(c, c), []).append(c)
    return out


def drop(codes):
    """The codes without the test branches (a calculation over every branch)."""
    s = sources()
    return [c for c in codes if c not in s]
