"""Waste of the portal beside the waste in ESB, per outlet (Waste Monitor tab "Portal vs ESB", 2026-11-13).

Management wants the waste of the portal and of ESB in step. Three records of a loss exist and nothing yet tells when
two of them are the same loss:

    portal     the waste entered by the outlets (integration_portal.waste_entry): approved / waiting, valued as the
               Waste Monitor (app/waste.py with_values: Pricelist, else ESB HPP)
    journal    the ESB item journals of the outlet (waste, QC and other stock adjustments), Σ |qty × price|
    bills      the ESB POS other-cost bills paid with a WASTE method: menus made and thrown away (their materials are
               in the system usage of the recipe cards already)
    same       a material in the portal waste and in an ESB item journal of the same outlet and day: maybe one loss
               recorded twice (the Purchase Estimate and COGS & Usage take the portal waste; check before adding the
               journal to anything)
"""
from datetime import date
from typing import Optional

from app import database as db
from app.database import SCHEMA

SKIP = ("New", "Draft", "Rejected", "Cancelled", "Canceled", "Void")


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def compare(d_from: date, d_to: date, branches: Optional[list[str]]) -> dict:
    from app import waste
    from app.pic_checkin import _names
    out: dict[str, dict] = {}

    def at(code: str) -> dict:
        return out.setdefault(code, {"branchCode": code, "portalApproved": 0.0, "portalWaiting": 0.0, "portalEntries": 0, "portalUnpriced": 0,
                                     "journalValue": 0.0, "journalLines": 0, "billPortions": 0.0, "bills": 0, "same": []})
    where = "business_date BETWEEN %s AND %s AND status <> 'rejected'" + ("" if branches is None else " AND branch_code = ANY(%s)")
    args = (d_from, d_to) + (() if branches is None else (branches,))
    portal_keys: dict[tuple, str] = {}
    for v in waste.with_values(db.fetch(f"SELECT {waste.LIGHT} FROM {waste.T} WHERE {where}", args)):
        b = at(v["branchCode"])
        b["portalEntries"] += 1
        if v.get("value") is None:
            b["portalUnpriced"] += 1
        elif v["status"] == "approved":
            b["portalApproved"] += v["value"]
        else:
            b["portalWaiting"] += v["value"]
        if v["scope"] != "premix" and v.get("itemCode"):
            portal_keys[(v["branchCode"], v["businessDate"], v["itemCode"])] = v.get("itemName") or v["itemCode"]
    journal_keys: set[tuple] = set()
    for r in db.fetch(
            f"""SELECT b.branch_code, d.doc_date, l.product_code, abs(coalesce(l.qty, 0) * coalesce(l.price, 0)) AS value
                  FROM {SCHEMA}.erp_documents d
                  JOIN {SCHEMA}.erp_document_lines l ON l.module = d.module AND l.doc_num = d.doc_num AND l.line_type = 'detail'
                  JOIN {SCHEMA}.master_branches b ON b.branch_id::text = d.branch_id::text
                 WHERE d.module = 'item_journal' AND d.doc_date BETWEEN %s AND %s AND d.deleted_at IS NULL
                   AND coalesce(d.status_name, '') <> ALL(%s) AND (%s::text[] IS NULL OR b.branch_code = ANY(%s))""",
            (d_from, d_to, list(SKIP), branches, branches)):
        b = at(r["branch_code"])
        b["journalValue"] += _f(r["value"])
        b["journalLines"] += 1
        if r["product_code"]:
            journal_keys.add((r["branch_code"], r["doc_date"].isoformat(), r["product_code"]))
    try:
        from app.aggregates import PAY_METHOD
        from app.database import TABLE_TRANSACTIONS
        from app.esb_report import TYPE_CONDITIONS, active_line_sql
        for r in db.fetch(
                f"""SELECT h.branch_code, count(DISTINCT h.sales_num)::int AS bills,
                           sum(coalesce(nullif(m->>'qty', '')::numeric, 0)) AS qty
                      FROM {TABLE_TRANSACTIONS} h, jsonb_array_elements(coalesce(h.raw_data->'salesMenus', '[]'::jsonb)) m
                     WHERE h.sales_date >= %(a)s::date AND h.sales_date < %(b)s::date + 1 AND {TYPE_CONDITIONS['other_cost']}
                       AND {active_line_sql('m')} AND upper({PAY_METHOD}) LIKE '%%WASTE%%'
                       AND (%(br)s::text[] IS NULL OR h.branch_code = ANY(%(br)s))
                     GROUP BY 1""", {"a": d_from, "b": d_to, "br": branches}):
            b = at(r["branch_code"])
            b["bills"] += int(r["bills"] or 0)
            b["billPortions"] += _f(r["qty"])
    except Exception:  # noqa: BLE001 - the transactions unreachable: the portal and the journals alone
        pass
    for key, name in portal_keys.items():
        if key in journal_keys and key[0] in out:
            out[key[0]]["same"].append({"day": key[1], "code": key[2], "name": name})
    names = _names(sorted(out)) if out else {}
    rows = []
    for code, b in out.items():
        b["branchName"] = names.get(code, code)
        b["sameCount"] = len(b["same"])
        b["same"] = sorted(b["same"], key=lambda x: (x["day"], x["name"]))[:30]
        for k in ("portalApproved", "portalWaiting", "journalValue", "billPortions"):
            b[k] = round(b[k], 2)
        rows.append(b)
    rows.sort(key=lambda x: (-x["sameCount"], -(x["portalApproved"] + x["journalValue"]), x["branchName"]))
    keys = ("portalApproved", "portalWaiting", "journalValue", "billPortions", "bills", "sameCount")
    return {"period": {"from": d_from.isoformat(), "to": d_to.isoformat()}, "data": rows,
            "total": {k: round(sum(r[k] for r in rows), 2) for k in keys}}
