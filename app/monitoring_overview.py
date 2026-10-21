"""Monitoring → Overview (2026-10-21): the PIC Check In, Monitoring Product and Monitoring Outlet of a period together,
per branch group (CCI, CTG, CTGP …, app/branch_groups.py) and per outlet: coverage (outlets visited), check-ins, visits,
scores, findings, the trend per day and the performance per category of both checklists."""
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app import branch_groups as bg
from app import pic_checkin
from app import visit_forms as vf

WIB = ZoneInfo("Asia/Jakarta")
LIMIT = 10000


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(WIB).date().isoformat()


def _avg(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def overview(date_from: date, date_to: date, scope: Optional[list[str]], group: Optional[str]) -> dict:
    groups = bg.list_groups()
    outlets = bg.outlets()
    if scope is not None:
        outlets = [o for o in outlets if o["code"] in set(scope)]
    if group:
        codes = set(bg.codes_of(group))
        outlets = [o for o in outlets if o["code"] in codes]
    branches = [o["code"] for o in outlets]

    checkins = pic_checkin.monitor(date_from, date_to, branches, limit=LIMIT)["data"] if branches else []
    forms = {f: vf.monitor(f, date_from, date_to, branches, limit=LIMIT) if branches else {"data": [], "sections": [], "findings": []}
             for f in ("product", "outlet")}

    per: dict[str, dict] = {o["code"]: {"code": o["code"], "name": o["name"], "groupId": o["group_id"], "groupCode": o["group_code"],
                                        "checkins": 0, "pics": set(), "outside": 0, "lastCheckin": None,
                                        "product": [], "outlet": [], "under": 0, "lastVisit": None} for o in outlets}
    trend: dict[str, dict] = {}

    def day(d: str) -> dict:
        return trend.setdefault(d, {"date": d, "checkins": 0, "product": 0, "outlet": 0, "productScores": [], "outletScores": []})

    for c in checkins:
        b = per.get(c["branchCode"])
        if not b:
            continue
        b["checkins"] += 1
        b["pics"].add(c["userId"])
        b["outside"] += 1 if c["outsideRadius"] else 0
        b["lastCheckin"] = max(b["lastCheckin"] or "", c["visitedAt"])
        day(_day(c["visitedAt"]))["checkins"] += 1
    for f in ("product", "outlet"):
        for v in forms[f]["data"]:
            b = per.get(v["branchCode"])
            if not b:
                continue
            if v["score"] is not None:
                b[f].append(v["score"])
            b["under"] += v["counts"]["under"]
            b["lastVisit"] = max(b["lastVisit"] or "", v["submittedAt"])
            t = day(_day(v["submittedAt"]))
            t[f] += 1
            if v["score"] is not None:
                t[f"{f}Scores"].append(v["score"])
            b.setdefault(f"{f}Visits", 0)
            b[f"{f}Visits"] = b.get(f"{f}Visits", 0) + 1

    rows = []
    for b in per.values():
        rows.append({
            "code": b["code"], "name": b["name"], "groupId": b["groupId"], "groupCode": b["groupCode"],
            "checkins": b["checkins"], "pics": len(b["pics"]), "outside": b["outside"], "lastCheckin": b["lastCheckin"],
            "productVisits": b.get("productVisits", 0), "productScore": _avg(b["product"]),
            "outletVisits": b.get("outletVisits", 0), "outletScore": _avg(b["outlet"]),
            "under": b["under"], "lastVisit": b["lastVisit"],
            "visited": b["checkins"] > 0 or b.get("productVisits", 0) > 0 or b.get("outletVisits", 0) > 0,
        })

    def summarize(items: list[dict]) -> dict:
        ps = [r["productScore"] for r in items if r["productScore"] is not None]
        os_ = [r["outletScore"] for r in items if r["outletScore"] is not None]
        return {"outlets": len(items), "visited": sum(1 for r in items if r["visited"]),
                "checkins": sum(r["checkins"] for r in items), "outside": sum(r["outside"] for r in items),
                "productVisits": sum(r["productVisits"] for r in items), "productScore": _avg(ps),
                "outletVisits": sum(r["outletVisits"] for r in items), "outletScore": _avg(os_),
                "under": sum(r["under"] for r in items)}

    by_group = []
    for g in groups["groups"]:
        items = [r for r in rows if r["groupId"] == g["id"]]
        if group and not items:
            continue
        by_group.append({"id": g["id"], "code": g["code"], "name": g["name"], **summarize(items)})
    none = [r for r in rows if not r["groupId"]]
    if none:
        by_group.append({"id": "none", "code": "—", "name": "Tanpa grup", **summarize(none)})

    pics = {c["userId"] for c in checkins} | {v["userId"] for f in ("product", "outlet") for v in forms[f]["data"]}
    days = sorted(trend.values(), key=lambda t: t["date"])
    return {
        "summary": {**summarize(rows), "pics": len(pics)},
        "groups": by_group,
        "branches": sorted(rows, key=lambda r: ((r["groupCode"] or "zzz"), r["name"])),
        "trend": [{"date": t["date"], "checkins": t["checkins"], "product": t["product"], "outlet": t["outlet"],
                   "productScore": _avg(t["productScores"]), "outletScore": _avg(t["outletScores"])} for t in days],
        "product": {"sections": forms["product"]["sections"], "findings": forms["product"]["findings"][:10]},
        "outlet": {"sections": forms["outlet"]["sections"], "findings": forms["outlet"]["findings"][:10]},
        "groupOptions": [{"id": g["id"], "code": g["code"], "name": g["name"], "branches": len(g["branches"])} for g in groups["groups"]],
    }
