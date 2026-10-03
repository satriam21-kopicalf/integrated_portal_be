"""Learn report master data from an ESB "Sales Recapitulation Detail Report" export.

The ESB API does not expose POS user names or branch cities, but the ESB
export shows them: Waiter (display name of the bill's POS user) and
Brand/City/Area per branch. This tool reads such an export, links its rows to
our transactions by Sales Number and updates

* integration_esb.master_pos_users        (user_name -> display_name)
* integration_esb.master_branch_attributes (brand, city, area per branch_code)

    python -m app.reference_import "docs/ESB_Sales Recapitulation Detail Report_01 Oktober 2026.xlsx" [more.xlsx] [--apply]

Without --apply it only prints what would change. The ESB export is the
reference, so differing values are overwritten; empty ESB values never erase ours.
"""
import argparse
import collections
import sys
from typing import Iterable

import openpyxl

from app import database as db
from app.database import SCHEMA, TABLE_TRANSACTIONS

HEADER_ROW = 11
CHUNK = 5000


def read_export(paths: Iterable[str]) -> tuple[dict, dict]:
    """(waiter votes per Sales Number, Brand/City/Area votes per Branch name)."""
    waiter: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    branch: dict[str, dict[str, collections.Counter]] = collections.defaultdict(
        lambda: {"brand": collections.Counter(), "city": collections.Counter(), "area": collections.Counter()})
    for path in paths:
        wb = openpyxl.load_workbook(path, read_only=True)
        for ws in wb.worksheets:
            col = None
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if col is None:
                    if i >= HEADER_ROW and row and row[0] == "Sales Number":
                        col = {name: k for k, name in enumerate(row) if name}
                    continue
                if not row or not row[0]:
                    continue
                if row[col["Waiter"]]:
                    waiter[row[0]][str(row[col["Waiter"]]).strip()] += 1
                b = branch[str(row[col["Branch"]]).strip()]
                for key, name in (("brand", "Brand"), ("city", "City"), ("area", "Area")):
                    if row[col[name]]:
                        b[key][str(row[col[name]]).strip()] += 1
        wb.close()
    return waiter, branch


def learn_users(waiter: dict) -> dict[str, str]:
    """createdBy of each sale (from our raw_data) -> the name ESB shows as Waiter."""
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    nums = list(waiter)
    for i in range(0, len(nums), CHUNK):
        for r in db.fetch(
            f"SELECT sales_num, raw_data->>'createdBy' AS user_name FROM {TABLE_TRANSACTIONS} WHERE sales_num = ANY(%s)",
            (nums[i:i + CHUNK],),
        ):
            if r["user_name"]:
                votes[r["user_name"]].update(waiter[r["sales_num"]])
    return {user: c.most_common(1)[0][0] for user, c in votes.items()}


def plan(waiter: dict, branch: dict) -> tuple[list, list]:
    users = learn_users(waiter)
    current_users = {r["user_name"]: r["display_name"] for r in db.fetch(f"SELECT user_name, display_name FROM {SCHEMA}.master_pos_users")}
    user_changes = [(u, current_users.get(u), name) for u, name in sorted(users.items()) if current_users.get(u) != name]

    codes = {r["branch_name"].strip(): r["branch_code"] for r in db.fetch(
        f"SELECT DISTINCT ON (branch_name) branch_name, branch_code FROM {SCHEMA}.master_branches "
        "WHERE COALESCE(branch_code, '') <> '' ORDER BY branch_name, COALESCE(is_deleted, false)")}
    attrs = {r["branch_code"]: r for r in db.fetch(f"SELECT branch_code, brand, city, area FROM {SCHEMA}.master_branch_attributes")}
    branch_changes = []
    for name, votes in sorted(branch.items()):
        code = codes.get(name)
        if not code:
            print(f"  ! branch not in master_branches: {name}", file=sys.stderr)
            continue
        cur = attrs.get(code) or {}
        new = {k: (v.most_common(1)[0][0] if v else None) for k, v in votes.items()}
        diff = {k: v for k, v in new.items() if v and v != cur.get(k)}
        if diff:
            branch_changes.append((code, name, {k: cur.get(k) for k in diff}, diff))
    return user_changes, branch_changes


def apply(user_changes: list, branch_changes: list) -> None:
    with db.transaction() as conn:
        for user, _, name in user_changes:
            conn.execute(
                f"""INSERT INTO {SCHEMA}.master_pos_users (user_name, display_name, updated_at) VALUES (%s, %s, now())
                    ON CONFLICT (user_name) DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = now()""",
                (user, name),
            )
        for code, _, _, diff in branch_changes:
            conn.execute(
                f"""INSERT INTO {SCHEMA}.master_branch_attributes (branch_code, brand, city, area, updated_at)
                    VALUES (%(code)s, %(brand)s, %(city)s, %(area)s, now())
                    ON CONFLICT (branch_code) DO UPDATE SET
                        brand = COALESCE(%(brand)s, {SCHEMA}.master_branch_attributes.brand),
                        city = COALESCE(%(city)s, {SCHEMA}.master_branch_attributes.city),
                        area = COALESCE(%(area)s, {SCHEMA}.master_branch_attributes.area),
                        updated_at = now()""",
                {"code": code, "brand": diff.get("brand"), "city": diff.get("city"), "area": diff.get("area")},
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("exports", nargs="+", help="ESB Sales Recapitulation Detail Report .xlsx files")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args(argv)
    db.open_pool(wait=True)
    try:
        waiter, branch = read_export(args.exports)
        print(f"read {len(waiter):,} sales and {len(branch)} branches from {len(args.exports)} file(s)")
        user_changes, branch_changes = plan(waiter, branch)
        print(f"\nPOS users to add/update: {len(user_changes)}")
        for user, old, new in user_changes:
            print(f"  {user:28s} {old or '(missing)'!s:34s} -> {new}")
        print(f"\nBranch attributes to update: {len(branch_changes)}")
        for code, name, old, new in branch_changes:
            print(f"  {code:6s} {name:40s} {old} -> {new}")
        if args.apply and (user_changes or branch_changes):
            apply(user_changes, branch_changes)
            print("\napplied")
        elif not args.apply:
            print("\ndry run (use --apply to write)")
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
