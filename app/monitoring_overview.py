"""Monitoring → Overview (2026-10-21): the PIC Check In, Monitoring Product and Monitoring Outlet of a period together,
per branch group (CCI, CTG, CTGP …, app/branch_groups.py) and per outlet: coverage (outlets visited), check-ins, visits,
scores, findings, the trend per day and the performance per category of both checklists. The assessment of every PIC
(2026-11-03): their check-in days, the average score of their own Product and Outlet visits and, from the SWAT audits
(Form WW-WR, app/wwwr.py), how far their ratings agree with the team's; per outlet the SWAT audits and open action plans.

The PIC score (2026-11-02, after the workbook "[Kopi Calf Group] [Masterfile] Store Performance"): the check-ins done
against the target (the outlets of the PIC × the check-ins a day of their branch group × the working days, 26 of 30),
the punctuality (the first check-in of the day before 11:00 WIB), the Product and Outlet visits done against the same
target and their scores, and the agreement with the SWAT audits. Weighted by WEIGHTS over the parts that have data;
the net sales of the outlets will join it later."""
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app import branch_groups as bg
from app import pic_checkin
from app import visit_forms as vf
from app import wwwr

WIB = ZoneInfo("Asia/Jakarta")
LIMIT = 10000
PUNCTUAL_BEFORE = 11          # the first check-in of the day before 11:00 WIB is on time (Store Performance)
WORKING_DAYS = 26 / 30        # 26 working days a month (Store Performance: daily × 26 = monthly)
# the parts of the PIC score (%), over the parts that have data; net sales to come
WEIGHTS = {"checkin": 15, "punctuality": 10, "visits": 15, "product": 15, "outlet": 15, "swat": 30}


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(WIB).date().isoformat()


def _avg(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _rate(done: float, target: float) -> Optional[float]:
    """done of target in %, at most 100 (None without a target)."""
    return round(min(100.0, done * 100 / target), 1) if target else None


def _score(parts: dict[str, Optional[float]]) -> Optional[float]:
    """The PIC score: the parts weighted by WEIGHTS over the parts that have data."""
    got = {k: v for k, v in parts.items() if v is not None}
    total = sum(WEIGHTS[k] for k in got)
    return round(sum(v * WEIGHTS[k] for k, v in got.items()) / total, 1) if total else None


def _assigned(user_ids: list[str]) -> dict[str, list[str]]:
    """The outlets assigned to each PIC (User Accounts → branches)."""
    if not user_ids:
        return {}
    from app import database as db
    rows = db.fetch("""SELECT user_id::text AS u, array_agg(branch_code ORDER BY branch_code) AS codes FROM integration_portal.user_branch
                        WHERE user_id::text = ANY(%s) GROUP BY 1""", (user_ids,))
    return {r["u"]: list(r["codes"]) for r in rows}


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

    # SWAT audits (WW-WR): per outlet the audits sent and the open action plans; per PIC the agreement with the team
    comp = wwwr.comparison(date_from, date_to, branches) if branches else {"audits": [], "pics": []}
    open_actions = {r["branch_code"]: r["n"] for r in wwwr.actions_of(branches)} if branches else {}
    swat_scores: dict[str, list[float]] = {}
    for a in comp["audits"]:
        if a["branchCode"] in per and a["score"] is not None:
            swat_scores.setdefault(a["branchCode"], []).append(a["score"])

    # the assessment per PIC
    people: dict[str, dict] = {}

    def person(uid: str, name: str) -> dict:
        return people.setdefault(uid, {"id": uid, "name": name, "checkinDays": set(), "checkins": 0, "outside": 0, "branches": set(),
                                       "product": [], "outlet": [], "productVisits": 0, "outletVisits": 0, "under": 0})
    first: dict[tuple[str, str], datetime] = {}
    for c in checkins:
        if c["branchCode"] not in per:
            continue
        x = person(c["userId"], c["name"])
        x["checkins"] += 1
        d = _day(c["visitedAt"])
        x["checkinDays"].add(d)
        at = datetime.fromisoformat(c["visitedAt"]).astimezone(WIB)
        if (c["userId"], d) not in first or at < first[(c["userId"], d)]:
            first[(c["userId"], d)] = at
        x["outside"] += 1 if c["outsideRadius"] else 0
        x["branches"].add(c["branchCode"])
    for f in ("product", "outlet"):
        for v in forms[f]["data"]:
            if v["branchCode"] not in per:
                continue
            x = person(v["userId"], v["name"])
            x[f"{f}Visits"] += 1
            x["under"] += v["counts"]["under"]
            x["branches"].add(v["branchCode"])
            if v["score"] is not None:
                x[f].append(v["score"])
    swat_of = {p["id"]: p for p in comp["pics"]}
    assigned = _assigned(list(people))
    limits = pic_checkin.daily_limits(sorted({c for cs in assigned.values() for c in cs} | {c for x in people.values() for c in x["branches"]}))
    today = datetime.now(WIB).date()
    days = max(0, (min(date_to, today) - date_from).days + 1)
    work = max(1, round(days * WORKING_DAYS)) if days else 0
    pic_rows = []
    for x in people.values():
        sw = swat_of.get(x["id"])
        outlets_of = [c for c in (assigned.get(x["id"]) or sorted(x["branches"])) if c in per]
        target = sum(limits.get(c, 1) for c in outlets_of) * work
        visit_target = len(outlets_of) * work
        punctual = sum(1 for (u, _), at in first.items() if u == x["id"] and at.hour < PUNCTUAL_BEFORE)
        parts = {"checkin": _rate(x["checkins"], target), "punctuality": _rate(punctual, len(x["checkinDays"])),
                 "visits": _rate(x["productVisits"] + x["outletVisits"], 2 * visit_target),
                 "product": _avg(x["product"]), "outlet": _avg(x["outlet"]), "swat": sw["conformity"] if sw else None}
        pic_rows.append({"id": x["id"], "name": x["name"], "branches": len(x["branches"]), "checkins": x["checkins"], "checkinDays": len(x["checkinDays"]),
                         "outside": x["outside"], "productVisits": x["productVisits"], "productScore": _avg(x["product"]),
                         "outletVisits": x["outletVisits"], "outletScore": _avg(x["outlet"]), "under": x["under"],
                         "swatAudits": sw["audits"] if sw else 0, "conformity": sw["conformity"] if sw else None,
                         "overrated": sw["overrated"] if sw else 0,
                         "openActions": sum(open_actions.get(b, 0) for b in x["branches"]),
                         "outlets": len(outlets_of), "checkinTarget": target, "checkinRate": parts["checkin"],
                         "punctualDays": punctual, "punctuality": parts["punctuality"], "visitTarget": 2 * visit_target,
                         "visitRate": parts["visits"], "score": _score(parts)})
    pic_rows.sort(key=lambda r: (r["score"] is None, r["score"] or 0, r["name"].lower()))

    rows = []
    for b in per.values():
        rows.append({
            "code": b["code"], "name": b["name"], "groupId": b["groupId"], "groupCode": b["groupCode"],
            "checkins": b["checkins"], "pics": len(b["pics"]), "outside": b["outside"], "lastCheckin": b["lastCheckin"],
            "productVisits": b.get("productVisits", 0), "productScore": _avg(b["product"]),
            "outletVisits": b.get("outletVisits", 0), "outletScore": _avg(b["outlet"]),
            "under": b["under"], "lastVisit": b["lastVisit"],
            "swatAudits": len(swat_scores.get(b["code"], [])), "swatScore": _avg(swat_scores.get(b["code"], [])), "openActions": open_actions.get(b["code"], 0),
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
        "picAssessment": pic_rows, "weights": WEIGHTS, "workingDays": work,
        "swat": {"audits": len(comp["audits"]), "conformity": comp.get("summary", {}).get("conformity"),
                 "overrated": comp.get("summary", {}).get("overrated", 0), "openActions": sum(open_actions.values())},
        "groupOptions": [{"id": g["id"], "code": g["code"], "name": g["name"], "branches": len(g["branches"])} for g in groups["groups"]],
    }
