"""Social Media Analytics — the listening engine (migration 135, 2026-11-23). What the public says about Kopi Calf,
collected by the portal itself on our VPS into our database: no API key, no token, no third-party scraping service.

Only what is open to the public without a login, asked politely (a pause between requests, no proxy, no fake accounts,
no disguised bot):

    youtube   the videos found by a search ("Kopi Calf", newest first): title, channel, views (yt-dlp; the video page
              asks a login from a data centre, so YouTube likes and comments are not collected)
    news      Google News RSS: articles on the web that name Kopi Calf
    tiktok    the videos of the official account with their likes, comments, views and shares (yt-dlp); the comment
              texts need a login (TikTok returns none), so TIKTOK_COMMENT_VIDEOS = 0

Not possible without a login (deliberately not built): searching Instagram, Threads, X, LinkedIn and TikTok.

Schedule (migration 136, social_schedule): one row per source — on / off, every how many hours, the last and the next
run, and a backfill date (the first runs reach back to 1 January 2026). A background thread checks every 5 minutes which
source is due and runs it (rows claimed, so one worker per source); a superadmin / admin may change the schedule, run a
source now or ask a backfill from the page. Each run logs a row in social_run (with its backfill date). A post gets a simple sentiment from an
Indonesian / English word list (a first signal, not a model).
"""
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote

import requests

from app import database as db
from app.utils import today as today_wib

logger = logging.getLogger("social")
P = "integration_portal"
# the keywords searched (YouTube, Google News) and the labels of a post (2026-11-23, migration 137): each label with the
# spellings that count for it; a post names at least one, else it only matched loosely and is dropped
LABELS: dict[str, tuple[str, ...]] = {
    "Kopi Calf": ("kopi calf", "kopicalf", "#kopicalf"),
    "Calf Coffee Industry": ("calf coffee industry", "calf coffee"),
    "Calf On The Road": ("calf on the road", "calfontheroad", "#cotr"),
    "Calf To Go": ("calf to go", "calftogo"),
    "Danang Yuda Prawira": ("danang yuda prawira", "danang yuda"),
    "Ridzky Novasandro": ("ridzky novasandro", "ridzky"),
    "Founder Kopi Calf": ("founder kopi calf", "founder calf"),
    "Co-Founder Kopi Calf": ("co-founder kopi calf", "cofounder kopi calf", "co founder kopi calf"),
    "Teman Calf": ("teman calf", "temancalf", "#temancalf"),
}
KEYWORDS = list(LABELS)
YOUTUBE_RESULTS = 30
YOUTUBE_COMMENTS = 40
TIKTOK_ACCOUNTS = ["kopicalf"]
TIKTOK_VIDEOS = 40
PAUSE = 2.0
UA = "Mozilla/5.0 (compatible; internal-monitor/1.0)"
FIRST_S = 120             # the first check after a worker starts
TICK_S = 300              # then every 5 minutes: which source is due (social_schedule, migration 136)
YOUTUBE_BACKFILL = 200    # search results read by a backfill
TIKTOK_BACKFILL = 400     # videos of the account read by a backfill
TIKTOK_COMMENT_VIDEOS = 0   # comment texts: TikTok returns none without a login (2026-11-23, JS challenge, empty list); counts are kept
TIKTOK_COMMENTS = 50      # comments read per video
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


def labels(*texts: Optional[str]) -> list[str]:
    """Every keyword a text names (its labels), in the order of LABELS; "Co-Founder" is not counted as "Founder" too."""
    t = " ".join(x or "" for x in texts).lower()
    out = [k for k, spellings in LABELS.items() if any(w in t for w in spellings)]
    if "Co-Founder Kopi Calf" in out and "Founder Kopi Calf" in out and not re.search(r"(?<!co-)(?<!co )(?<!co)founder (kopi )?calf", t):
        out.remove("Founder Kopi Calf")
    return out


def relevant(*texts: Optional[str]) -> bool:
    return bool(labels(*texts))


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

def youtube(kw: str, since: Optional[date] = None) -> list[dict]:
    """The search page of YouTube (newest first). From our VPS YouTube opens the search but asks a login for a video page
    ("Sign in to confirm you're not a bot", 2026-11-23), so only what the search shows is kept: title, description,
    channel, views (likes and comments of YouTube need the video page: not collected). The upload date comes from the
    public RSS of the channel (its last 15 videos), else it stays unknown. A backfill reads more of the search."""
    with _ydl({"extract_flat": True, "playlistend": YOUTUBE_BACKFILL if since else YOUTUBE_RESULTS}) as y:
        found = y.extract_info(f"https://www.youtube.com/results?search_query={quote(kw)}&sp=CAI%3D", download=False) or {}
    out: list[dict] = []
    feeds: dict[str, dict[str, datetime]] = {}
    for e in found.get("entries") or []:
        vid = (e or {}).get("id") or ""
        if len(vid) != 11 or not relevant(e.get("title"), e.get("description")):
            continue  # a channel or a playlist, or a video that only matched loosely
        ch = e.get("channel_id")
        if ch and ch not in feeds:
            feeds[ch] = _channel_dates(ch)
        at = (feeds.get(ch) or {}).get(vid)
        if since and at and at.date() < since:
            continue
        out.append(_row("youtube", "search", kw, vid, e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                        e.get("channel") or e.get("uploader"), f"{e.get('title', '')}\n\n{e.get('description') or ''}", at,
                        None, None, e.get("view_count"), None,
                        {k: e.get(k) for k in ("title", "channel", "channel_id", "duration", "uploader_id")}))
    return out


def _channel_dates(channel_id: str) -> dict[str, datetime]:
    """{video id: published} of the last 15 videos of a channel (the public RSS feed of YouTube)."""
    try:
        r = _http.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=20)
        r.raise_for_status()
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        out = {}
        for en in ET.fromstring(r.content).findall("a:entry", ns):
            vid, pub = en.findtext("yt:videoId", namespaces=ns), en.findtext("a:published", namespaces=ns)
            if vid and pub:
                out[vid] = datetime.fromisoformat(pub)
        time.sleep(PAUSE / 2)
        return out
    except Exception:  # noqa: BLE001 - no date then
        return {}


def news(kw: str, since: Optional[date] = None) -> list[dict]:
    """Google News RSS. A backfill asks month by month (after: / before:), the feed giving at most ~100 a query."""
    windows: list = [None]
    if since:
        windows, d = [], since.replace(day=1)
        while d <= today_wib():
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            windows.append((d, nxt))
            d = nxt
    out = []
    for w in windows:
        q = f'"{kw}"' + (f" after:{w[0].isoformat()} before:{w[1].isoformat()}" if w else "")
        r = _http.get(f"https://news.google.com/rss/search?q={quote(q)}&hl=id&gl=ID&ceid=ID:id", timeout=30)
        r.raise_for_status()
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


def tiktok(user: str, since: Optional[date] = None) -> list[dict]:
    """The videos of the official account with their likes, comments, views and shares; the comments of the newest
    TIKTOK_COMMENT_VIDEOS videos (up to TIKTOK_COMMENTS each). A backfill reads back to `since`. Skipped when TikTok
    refuses without a login."""
    import yt_dlp
    opts: dict = {"playlistend": TIKTOK_BACKFILL if since else TIKTOK_VIDEOS}
    if since:
        opts["daterange"] = yt_dlp.utils.DateRange(since.strftime("%Y%m%d"), None)
    with _ydl(opts) as y:
        d = y.extract_info(f"https://www.tiktok.com/@{user}", download=False) or {}
    vids = [v for v in d.get("entries") or [] if v and v.get("id")]
    out = [_row("tiktok", "account", "@" + user, v.get("id"), v.get("webpage_url"), user, v.get("description") or v.get("title"),
                _day(v.get("upload_date")), v.get("like_count"), v.get("comment_count"), v.get("view_count"), v.get("repost_count"),
                {k: v.get(k) for k in ("title", "duration")})
           for v in vids]
    with _ydl({"getcomments": True, "extractor_args": {"tiktok": {"max_comments": [str(TIKTOK_COMMENTS)]}}}) as y:
        for v in vids[:TIKTOK_COMMENT_VIDEOS]:
            if not v.get("comment_count"):
                continue
            try:
                full = y.extract_info(v.get("webpage_url"), download=False) or {}
            except Exception as e:  # noqa: BLE001 - comments refused: the video stays, its comments are skipped
                logger.info("tiktok comments %s: %s", v.get("id"), str(e)[:120])
                continue
            for c in (full.get("comments") or [])[:TIKTOK_COMMENTS]:
                at = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc) if c.get("timestamp") else None
                out.append(_row("tiktok", "comment", "@" + user, f"{v.get('id')}:{c.get('id')}", v.get("webpage_url"),
                                c.get("author"), c.get("text"), at, c.get("like_count"), None, None, None,
                                {"video": v.get("id"), "title": (v.get("description") or "")[:120]}))
            time.sleep(PAUSE)
    return out


def collect(source: str, since: Optional[date] = None) -> list[dict]:
    if source == "youtube":
        return [r for k in KEYWORDS for r in youtube(k, since)]
    if source == "news":
        return [r for k in KEYWORDS for r in news(k, since)]
    if source == "tiktok":
        return [r for u in TIKTOK_ACCOUNTS for r in tiktok(u, since)]
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
                                                  views, shares, sentiment, keywords, raw)
                    VALUES (%(platform)s, %(post_id)s, %(source)s, %(keyword)s, %(url)s, %(author)s, %(text)s, %(created_at)s,
                            %(likes)s, %(comments)s, %(views)s, %(shares)s, %(sentiment)s, %(keywords)s, %(raw)s::jsonb)
                    ON CONFLICT (platform, post_id) DO UPDATE SET likes = coalesce(EXCLUDED.likes, {P}.social_post.likes),
                        comments = coalesce(EXCLUDED.comments, {P}.social_post.comments),
                        views = coalesce(EXCLUDED.views, {P}.social_post.views), shares = coalesce(EXCLUDED.shares, {P}.social_post.shares),
                        created_at = coalesce({P}.social_post.created_at, EXCLUDED.created_at),
                        keywords = ARRAY(SELECT DISTINCT unnest({P}.social_post.keywords || EXCLUDED.keywords)), last_seen = now()
                    RETURNING (xmax = 0) AS inserted""",
                {**r, "sentiment": sentiment(r["text"]), "keywords": labels(r["text"], r["author"]) or ([r["keyword"]] if r["keyword"] in LABELS else []),
                 "raw": json.dumps(r["raw"], ensure_ascii=False, default=str)}).fetchone()
            added += bool(got and got["inserted"])
    return added


def run_source(source: str, since: Optional[date] = None) -> dict:
    """One source, logged in social_run; never raises."""
    rid = db.fetchrow(f"INSERT INTO {P}.social_run (source, backfill_from) VALUES (%s, %s) RETURNING id", (source, since))["id"]
    found, added, err = None, None, None
    try:
        rows = collect(source, since)
        found, added = len(rows), save(rows)
    except Exception as e:  # noqa: BLE001
        err = str(e)[:500]
        logger.info("social %s: %s", source, err)
    with db.transaction() as conn:
        conn.execute(f"UPDATE {P}.social_run SET finished_at = now(), found = %s, added = %s, error = %s WHERE id = %s",
                     (found, added, err, rid))
    return {"source": source, "found": found, "added": added, "error": err}


def tick() -> list[dict]:
    """One check of the schedule: every due source runs (a backfill when one is set), then gets its next time. The due
    rows are claimed first (their next time moved 30 minutes on, FOR UPDATE SKIP LOCKED), so two workers never run the
    same source; a source that failed is tried again at its next time like any other."""
    with db.transaction() as conn:
        rows = conn.execute(f"""UPDATE {P}.social_schedule s SET next_run_at = now() + interval '30 minutes'
                                 WHERE s.source IN (SELECT source FROM {P}.social_schedule WHERE enabled AND next_run_at <= now()
                                                     FOR UPDATE SKIP LOCKED)
                                 RETURNING s.source, s.backfill_from""").fetchall()
    out = []
    for r in rows:
        if r["source"] not in SOURCES:
            continue
        res = run_source(r["source"], r["backfill_from"])
        with db.transaction() as conn:
            conn.execute(f"""UPDATE {P}.social_schedule SET last_run_at = now(), next_run_at = now() + every_hours * interval '1 hour',
                                    backfill_from = CASE WHEN %s THEN NULL ELSE backfill_from END WHERE source = %s""",
                         (res["error"] is None, r["source"]))
        out.append(res)
    return out


def relabel() -> int:
    """The posts kept before the labels (migration 137) get theirs from their text."""
    rows = db.fetch(f"SELECT platform, post_id, text, author, keyword FROM {P}.social_post WHERE keywords = '{{}}'")
    with db.transaction() as conn:
        for r in rows:
            k = labels(r["text"], r["author"]) or ([r["keyword"]] if r["keyword"] in LABELS else ["Kopi Calf"])
            conn.execute(f"UPDATE {P}.social_post SET keywords = %s WHERE platform = %s AND post_id = %s", (k, r["platform"], r["post_id"]))
    return len(rows)


def _loop() -> None:
    time.sleep(FIRST_S)
    try:
        relabel()
    except Exception as e:  # noqa: BLE001
        logger.warning("social relabel: %s", e)
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001 - the loop never dies
            logger.warning("social scheduler: %s", e)
        time.sleep(TICK_S)


def start() -> None:
    threading.Thread(target=_loop, name="social-scheduler", daemon=True).start()


def run_now(source: Optional[str] = None, backfill_from: Optional[date] = None) -> None:
    """From the page (superadmin / admin): every source (or one) due now, a backfill from a date when given."""
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.social_schedule SET next_run_at = now(), backfill_from = coalesce(%s, backfill_from)
                         WHERE %s::text IS NULL OR source = %s""", (backfill_from, source, source))
    threading.Thread(target=tick, name="social-run-now", daemon=True).start()


def schedule() -> list[dict]:
    return [{"source": r["source"], "enabled": r["enabled"], "everyHours": float(r["every_hours"]),
             "lastRunAt": r["last_run_at"].isoformat() if r["last_run_at"] else None, "nextRunAt": r["next_run_at"].isoformat(),
             "backfillFrom": r["backfill_from"].isoformat() if r["backfill_from"] else None}
            for r in db.fetch(f"SELECT * FROM {P}.social_schedule ORDER BY source")]


def set_schedule(user: dict, source: str, enabled: bool, every_hours: float) -> list[dict]:
    if source not in SOURCES:
        raise ValueError("Sumber tidak dikenal")
    if not 1 <= every_hours <= 168:
        raise ValueError("Interval 1 sampai 168 jam")
    with db.transaction() as conn:
        conn.execute(f"""UPDATE {P}.social_schedule SET enabled = %s, every_hours = %s, updated_by = %s, updated_at = now(),
                                next_run_at = least(next_run_at, coalesce(last_run_at, now()) + %s * interval '1 hour')
                         WHERE source = %s""", (enabled, every_hours, user.get("full_name") or user.get("username"), every_hours, source))
    return schedule()


# ---------------------------------------------------------------- reads (the page)

def posts(platform: Optional[str], q: str, sentiment_: Optional[str], d_from, d_to, limit: int, offset: int, keyword: Optional[str] = None) -> dict:
    where = ["coalesce(created_at, first_seen) >= %(a)s", "coalesce(created_at, first_seen) < %(b)s + interval '1 day'"]
    p: dict = {"a": d_from, "b": d_to, "lim": limit, "off": offset}
    if platform:
        where.append("platform = %(pf)s"); p["pf"] = platform
    if sentiment_:
        where.append("sentiment = %(se)s"); p["se"] = sentiment_
    if q.strip():
        where.append("(text ILIKE %(q)s OR author ILIKE %(q)s)"); p["q"] = f"%{q.strip()}%"
    if keyword:
        where.append("%(kw)s = ANY(keywords)"); p["kw"] = keyword
    w = " AND ".join(where)
    rows = db.fetch(f"""SELECT platform, post_id, source, keyword, url, author, left(text, 600) AS text, created_at, likes, comments, keywords,
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
    return {"schedule": schedule(), "platforms": per, "daily": [{"day": r["day"].isoformat(), "posts": r["posts"]} for r in daily], "authors": authors,
            "runs": [{**r, "startedAt": r["started_at"].isoformat(), "finishedAt": r["finished_at"].isoformat() if r["finished_at"] else None}
                     for r in runs]}


def post(platform: str, post_id: str) -> Optional[dict]:
    """One mention in full (the preview of the page): the whole text, every figure, the dates and what was kept of it."""
    r = db.fetchrow(f"SELECT * FROM {P}.social_post WHERE platform = %s AND post_id = %s", (platform, post_id))
    if not r:
        return None
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {"platform": r["platform"], "post_id": r["post_id"], "source": r["source"], "keyword": r["keyword"], "url": r["url"],
            "author": r["author"], "text": r["text"], "createdAt": iso(r["created_at"]), "firstSeen": iso(r["first_seen"]),
            "lastSeen": iso(r["last_seen"]), "likes": r["likes"], "comments": r["comments"], "views": r["views"], "shares": r["shares"],
            "sentiment": r["sentiment"], "keywords": r["keywords"] or [], "raw": r["raw"] or {}}



def overview(d_from, d_to) -> dict:
    """The analysis of the period (the Overview page): per month and platform the mentions, views, likes and comments;
    the sentiment per platform; the accounts that reached most; the posts with the most views."""
    p = {"a": d_from, "b": d_to}
    rng = "coalesce(created_at, first_seen) >= %(a)s AND coalesce(created_at, first_seen) < %(b)s + interval '1 day'"
    month = "to_char(coalesce(created_at, first_seen) AT TIME ZONE 'Asia/Jakarta', 'YYYY-MM')"
    monthly = db.fetch(f"""SELECT {month} AS month, platform, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views,
                                  coalesce(sum(likes), 0)::bigint AS likes, coalesce(sum(comments), 0)::bigint AS comments
                             FROM {P}.social_post WHERE {rng} AND source <> 'comment' GROUP BY 1, 2 ORDER BY 1, 2""", p)
    feel = db.fetch(f"""SELECT platform, sentiment, count(*)::int AS n FROM {P}.social_post WHERE {rng} GROUP BY 1, 2""", p)
    authors = db.fetch(f"""SELECT author, platform, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views,
                                  coalesce(sum(likes), 0)::bigint AS likes
                             FROM {P}.social_post WHERE {rng} AND author IS NOT NULL AND source <> 'comment'
                            GROUP BY 1, 2 ORDER BY 4 DESC, 3 DESC LIMIT 10""", p)
    top = db.fetch(f"""SELECT platform, post_id, author, left(text, 160) AS text, views, likes, comments, created_at
                         FROM {P}.social_post WHERE {rng} AND views IS NOT NULL ORDER BY views DESC LIMIT 10""", p)
    per_kw = db.fetch(f"""SELECT k AS keyword, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views
                            FROM {P}.social_post, unnest(keywords) k WHERE {rng} GROUP BY 1 ORDER BY 2 DESC""", p)
    tot = db.fetchrow(f"""SELECT count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views, coalesce(sum(likes), 0)::bigint AS likes,
                                 coalesce(sum(comments), 0)::bigint AS comments,
                                 count(*) FILTER (WHERE sentiment = 'positive')::int AS positive,
                                 count(*) FILTER (WHERE sentiment = 'negative')::int AS negative,
                                 count(DISTINCT author)::int AS authors
                            FROM {P}.social_post WHERE {rng}""", p)
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "total": tot, "keywords": per_kw, "monthly": monthly, "sentiment": feel, "authors": authors,
            "top": [{**r, "createdAt": r["created_at"].isoformat() if r["created_at"] else None, "created_at": None} for r in top]}
