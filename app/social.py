"""Social Media Analytics — the listening engine (migration 135, 2026-11-23). What the public says about Kopi Calf,
collected by the portal itself on our VPS into our database: no API key, no token, no third-party scraping service.

Only what is open to the public without a login, asked politely (a pause between requests, no proxy, no fake accounts,
no disguised bot):

    youtube   the videos found by a search ("Kopi Calf", newest first) with their figures, and their top comments (yt-dlp)
    news      Google News RSS: articles on the web that name Kopi Calf
    tiktok    the videos of the official account (yt-dlp); skipped when TikTok refuses without a login

Not possible without a login (deliberately not built): searching Instagram, Threads, X, LinkedIn and TikTok.

A background thread of every worker (one at a time: pg advisory lock) runs every SOCIAL_EVERY_H hours; a superadmin /
admin may start a run from the page. Each source logs a row in social_run. A post gets a simple sentiment from an
Indonesian / English word list (a first signal, not a model).
"""
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

import requests

from app import database as db

logger = logging.getLogger("social")
P = "integration_portal"
KEYWORDS = ["Kopi Calf", "kopicalf"]
MATCH = ("kopi calf", "kopicalf", "calf coffee")      # a result must name Kopi Calf (drops what only matched loosely)
YOUTUBE_RESULTS = 30
YOUTUBE_COMMENTS = 40
TIKTOK_ACCOUNTS = ["kopicalf"]
TIKTOK_VIDEOS = 30
PAUSE = 2.0
UA = "Mozilla/5.0 (compatible; internal-monitor/1.0)"
EVERY_H = float(os.environ.get("SOCIAL_EVERY_H", "6"))
FIRST_S = 600
LOCK = 980_135
SOURCES = ("youtube", "news", "tiktok")  # reddit refuses anonymous search (403, 2026-11-23): left out

_http = requests.Session()
_http.headers["User-Agent"] = UA

POS = {"enak", "mantap", "mantab", "suka", "recommended", "rekomen", "favorit", "murah", "worth", "best", "terbaik", "nikmat", "segar",
       "seger", "keren", "puas", "ramah", "cepat", "lezat", "top", "love", "good", "great", "nice", "delicious", "affordable", "pas", "wenak"}
NEG = {"mahal", "pahit", "basi", "kecewa", "lama", "jelek", "buruk", "tidak enak", "gak enak", "ga enak", "nggak enak", "kurang",
       "hambar", "encer", "lambat", "kotor", "jutek", "zonk", "nyesel", "menyesal", "bad", "worst", "expensive", "disappointed", "slow"}


def sentiment(text: Optional[str]) -> str:
    """positive / negative / neutral from a word list (a first signal)."""
    t = f" {(text or '').lower()} "
    neg = 0
    for w in sorted(NEG, key=len, reverse=True):  # "gak enak" first, then taken out so its "enak" is not counted as good
        hits = len(re.findall(rf"\b{re.escape(w)}\b", t))
        if hits:
            neg += hits
            t = re.sub(rf"\b{re.escape(w)}\b", " ", t)
    pos = sum(1 for w in POS if re.search(rf"\b{re.escape(w)}\b", t))
    return "positive" if pos > neg else "negative" if neg > pos else "neutral"


def relevant(*texts: Optional[str]) -> bool:
    t = " ".join(x or "" for x in texts).lower()
    return any(m in t for m in MATCH)


def _row(platform, source, keyword, pid, url, author, text, created, likes=None, comments=None, views=None, shares=None, raw=None) -> dict:
    return {"platform": platform, "source": source, "keyword": keyword, "post_id": str(pid), "url": url, "author": author,
            "text": (text or "")[:5000], "created_at": created, "likes": likes, "comments": comments, "views": views,
            "shares": shares, "raw": raw or {}}


def _day(d: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ydl(opts: dict):
    import yt_dlp
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True, **opts})


# ---------------------------------------------------------------- sources

def youtube(kw: str, comments: bool = True) -> list[dict]:
    with _ydl({"extract_flat": True}) as y:
        # the search page of YouTube sorted by upload date (sp=CAI%3D); ytsearchdate is not known to every yt-dlp
        found = y.extract_info(f"https://www.youtube.com/results?search_query={quote(kw)}&sp=CAI%3D", download=False) or {}
    ids = [e.get("id") for e in found.get("entries") or [] if e and e.get("id")][:YOUTUBE_RESULTS]
    opts = {"getcomments": True, "extractor_args": {"youtube": {"max_comments": [str(YOUTUBE_COMMENTS)]}}} if comments else {}
    out: list[dict] = []
    with _ydl(opts) as y:
        for vid in ids:
            try:
                v = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False) or {}
            except Exception as e:  # noqa: BLE001 - one video failing never stops the run
                logger.info("youtube %s: %s", vid, str(e)[:120])
                continue
            if not relevant(v.get("title"), v.get("description"), " ".join(v.get("tags") or [])):
                continue
            out.append(_row("youtube", "search", kw, vid, v.get("webpage_url"), v.get("channel") or v.get("uploader"),
                            f"{v.get('title', '')}\n\n{v.get('description', '')}", _day(v.get("upload_date")),
                            v.get("like_count"), v.get("comment_count"), v.get("view_count"), None,
                            {k: v.get(k) for k in ("title", "channel", "channel_id", "duration", "tags")}))
            for c in v.get("comments") or []:
                at = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc) if c.get("timestamp") else None
                out.append(_row("youtube", "comment", kw, f"{vid}:{c.get('id')}", f"{v.get('webpage_url')}&lc={c.get('id')}",
                                c.get("author"), c.get("text"), at, c.get("like_count"), None, None, None,
                                {"video": vid, "title": v.get("title")}))
            time.sleep(PAUSE)
    return out


def news(kw: str) -> list[dict]:
    r = _http.get(f"https://news.google.com/rss/search?q={quote(chr(34) + kw + chr(34))}&hl=id&gl=ID&ceid=ID:id", timeout=30)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title, link, src = it.findtext("title"), it.findtext("link"), it.find("source")
        try:
            created = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            created = None
        out.append(_row("news", "google-news", kw, link, link, src.text if src is not None else None, title, created,
                        raw={"site": src.get("url") if src is not None else None}))
    time.sleep(PAUSE)
    return out


def reddit(kw: str) -> list[dict]:
    r = _http.get("https://www.reddit.com/search.json", params={"q": f'"{kw}"', "sort": "new", "limit": 50}, timeout=30)
    if r.status_code in (403, 429):  # refused: skipped, never forced
        raise RuntimeError(f"Reddit menolak ({r.status_code})")
    r.raise_for_status()
    out = []
    for ch in r.json().get("data", {}).get("children", []):
        p = ch.get("data", {})
        if not relevant(p.get("title"), p.get("selftext")):
            continue
        at = datetime.fromtimestamp(p["created_utc"], tz=timezone.utc) if p.get("created_utc") else None
        out.append(_row("reddit", "search", kw, p.get("id"), f"https://www.reddit.com{p.get('permalink', '')}", p.get("author"),
                        f"{p.get('title', '')}\n\n{p.get('selftext', '')}", at, p.get("ups"), p.get("num_comments"),
                        raw={"subreddit": p.get("subreddit")}))
    time.sleep(PAUSE)
    return out


def tiktok(user: str) -> list[dict]:
    with _ydl({"playlistend": TIKTOK_VIDEOS}) as y:
        d = y.extract_info(f"https://www.tiktok.com/@{user}", download=False) or {}
    return [_row("tiktok", "account", "@" + user, v.get("id"), v.get("webpage_url"), user, v.get("description") or v.get("title"),
                 _day(v.get("upload_date")), v.get("like_count"), v.get("comment_count"), v.get("view_count"), v.get("repost_count"),
                 {k: v.get(k) for k in ("title", "duration")})
            for v in d.get("entries") or [] if v and v.get("id")]


def collect(source: str) -> list[dict]:
    if source == "youtube":
        return [r for k in KEYWORDS for r in youtube(k)]
    if source == "news":
        return [r for k in KEYWORDS for r in news(k)]
    if source == "reddit":
        return [r for k in KEYWORDS for r in reddit(k)]
    if source == "tiktok":
        return [r for u in TIKTOK_ACCOUNTS for r in tiktok(u)]
    raise ValueError(source)


# ---------------------------------------------------------------- store

def save(rows: list[dict]) -> int:
    """Upsert: a new post is added with its sentiment, a known one gets its figures and last_seen refreshed."""
    added = 0
    seen = set()
    with db.transaction() as conn:
        for r in rows:
            key = (r["platform"], r["post_id"])
            if not r["post_id"] or r["post_id"] == "None" or key in seen:
                continue
            seen.add(key)
            got = conn.execute(
                f"""INSERT INTO {P}.social_post (platform, post_id, source, keyword, url, author, text, created_at, likes, comments,
                                                  views, shares, sentiment, raw)
                    VALUES (%(platform)s, %(post_id)s, %(source)s, %(keyword)s, %(url)s, %(author)s, %(text)s, %(created_at)s,
                            %(likes)s, %(comments)s, %(views)s, %(shares)s, %(sentiment)s, %(raw)s::jsonb)
                    ON CONFLICT (platform, post_id) DO UPDATE SET likes = EXCLUDED.likes, comments = EXCLUDED.comments,
                        views = EXCLUDED.views, shares = EXCLUDED.shares, last_seen = now()
                    RETURNING (xmax = 0) AS inserted""",
                {**r, "sentiment": sentiment(r["text"]), "raw": json.dumps(r["raw"], ensure_ascii=False, default=str)}).fetchone()
            added += bool(got and got["inserted"])
    return added


def run(sources: tuple[str, ...] = SOURCES) -> list[dict]:
    """One run of every source, logged; one source failing never stops the others."""
    out = []
    for s in sources:
        rid = db.fetchrow(f"INSERT INTO {P}.social_run (source) VALUES (%s) RETURNING id", (s,))["id"]
        found, added, err = None, None, None
        try:
            rows = collect(s)
            found, added = len(rows), save(rows)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:500]
            logger.info("social %s: %s", s, err)
        with db.transaction() as conn:
            conn.execute(f"UPDATE {P}.social_run SET finished_at = now(), found = %s, added = %s, error = %s WHERE id = %s",
                         (found, added, err, rid))
        out.append({"source": s, "found": found, "added": added, "error": err})
    return out


def _loop() -> None:
    time.sleep(FIRST_S)
    while True:
        try:
            with db.transaction() as conn:
                ok = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]
            if ok:
                try:
                    last = db.fetchrow(f"SELECT max(started_at) AS at FROM {P}.social_run")
                    if not last or not last["at"] or datetime.now(timezone.utc) - last["at"] >= timedelta(hours=EVERY_H):
                        run()
                finally:
                    with db.transaction() as conn:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
        except Exception as e:  # noqa: BLE001 - the loop never dies
            logger.warning("social loop: %s", e)
        time.sleep(1800)


def start() -> None:
    threading.Thread(target=_loop, name="social-listening", daemon=True).start()


def run_now() -> None:
    """From the page (superadmin / admin): a run in the background."""
    threading.Thread(target=run, name="social-run-now", daemon=True).start()


# ---------------------------------------------------------------- reads (the page)

def posts(platform: Optional[str], q: str, sentiment_: Optional[str], d_from, d_to, limit: int, offset: int) -> dict:
    where = ["coalesce(created_at, first_seen) >= %(a)s", "coalesce(created_at, first_seen) < %(b)s + interval '1 day'"]
    p: dict = {"a": d_from, "b": d_to, "lim": limit, "off": offset}
    if platform:
        where.append("platform = %(pf)s"); p["pf"] = platform
    if sentiment_:
        where.append("sentiment = %(se)s"); p["se"] = sentiment_
    if q.strip():
        where.append("(text ILIKE %(q)s OR author ILIKE %(q)s)"); p["q"] = f"%{q.strip()}%"
    w = " AND ".join(where)
    rows = db.fetch(f"""SELECT platform, post_id, source, keyword, url, author, left(text, 600) AS text, created_at, likes, comments,
                               views, shares, sentiment, first_seen FROM {P}.social_post WHERE {w}
                        ORDER BY coalesce(created_at, first_seen) DESC LIMIT %(lim)s OFFSET %(off)s""", p)
    total = db.fetchrow(f"SELECT count(*)::int AS n FROM {P}.social_post WHERE {w}", p)["n"]
    return {"total": total, "data": [{**r, "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
                                      "firstSeen": r["first_seen"].isoformat()} for r in rows]}


def summary(d_from, d_to) -> dict:
    p = {"a": d_from, "b": d_to}
    rng = "coalesce(created_at, first_seen) >= %(a)s AND coalesce(created_at, first_seen) < %(b)s + interval '1 day'"
    per = db.fetch(f"""SELECT platform, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views,
                              coalesce(sum(likes), 0)::bigint AS likes, coalesce(sum(comments), 0)::bigint AS comments,
                              count(*) FILTER (WHERE sentiment = 'positive')::int AS positive,
                              count(*) FILTER (WHERE sentiment = 'negative')::int AS negative
                         FROM {P}.social_post WHERE {rng} GROUP BY 1 ORDER BY 2 DESC""", p)
    daily = db.fetch(f"""SELECT (coalesce(created_at, first_seen) AT TIME ZONE 'Asia/Jakarta')::date AS day, count(*)::int AS posts
                           FROM {P}.social_post WHERE {rng} GROUP BY 1 ORDER BY 1""", p)
    authors = db.fetch(f"""SELECT author, platform, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views
                             FROM {P}.social_post WHERE {rng} AND author IS NOT NULL GROUP BY 1, 2 ORDER BY 4 DESC, 3 DESC LIMIT 10""", p)
    runs = db.fetch(f"""SELECT DISTINCT ON (source) source, started_at, finished_at, found, added, error FROM {P}.social_run
                        ORDER BY source, started_at DESC""")
    return {"platforms": per, "daily": [{"day": r["day"].isoformat(), "posts": r["posts"]} for r in daily], "authors": authors,
            "runs": [{**r, "startedAt": r["started_at"].isoformat(), "finishedAt": r["finished_at"].isoformat() if r["finished_at"] else None}
                     for r in runs]}
