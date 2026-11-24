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
NEWS_PAUSE_BACKFILL = 5.0   # Google News answered 429 to a backfill at 2 s a request (2026-11-23)
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
    # Indonesian: without it YouTube answers a data centre in English and machine-translates the titles (2026-11-24)
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True,
                             "http_headers": {"Accept-Language": "id-ID,id;q=0.9"}, "extractor_args": {"youtube": {"lang": ["id"]}}, **opts})


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
    ago = _search_ages(kw)  # {video: "3mo ago"} of the first page of the search
    for e in found.get("entries") or []:
        vid = (e or {}).get("id") or ""
        if len(vid) != 11 or not relevant(e.get("title"), e.get("description")):
            continue  # a channel or a playlist, or a video that only matched loosely
        ch = e.get("channel_id")
        if ch and ch not in feeds:
            feeds[ch] = _channel_dates(ch)
        at = (feeds.get(ch) or {}).get(vid)
        approx = None
        if at is None and vid in ago:  # no exact date: the age the search shows, kept as approximate
            at, approx = _from_age(ago[vid]), ago[vid]
        if since and at and at.date() < since:
            continue
        out.append(_row("youtube", "search", kw, vid, e.get("url") or f"https://www.youtube.com/watch?v={vid}",
                        e.get("channel") or e.get("uploader"), f"{e.get('title', '')}\n\n{e.get('description') or ''}", at,
                        None, None, e.get("view_count"), None,
                        {**{k: e.get(k) for k in ("title", "channel", "channel_id", "duration", "uploader_id")},
                         **({"date_approx": True, "published_text": approx} if approx else {})}))
    return out


_AGE = re.compile(r"(\d+)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day|w|wk|week|mo|month|y|yr|year)s?\b", re.I)


def _from_age(text: str) -> Optional[datetime]:
    """"3mo ago" / "2 years ago" -> the moment it points to (approximate)."""
    m = _AGE.search(text or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    days = {"s": 0, "sec": 0, "second": 0, "m": 0, "min": 0, "minute": 0, "h": 0, "hr": 0, "hour": 0, "d": 1, "day": 1,
            "w": 7, "wk": 7, "week": 7, "mo": 30, "month": 30, "y": 365, "yr": 365, "year": 365}[u]
    hours = n if u in ("h", "hr", "hour") else 0
    return datetime.now(timezone.utc) - timedelta(days=n * days, hours=hours)


def _search_ages(kw: str) -> dict[str, str]:
    """{video id: "3mo ago"} from the search page of YouTube (its first page), no login needed."""
    try:
        r = _http.get("https://www.youtube.com/results", params={"search_query": kw, "sp": "CAI%3D", "hl": "en", "gl": "ID"},
                      headers={"Accept-Language": "en"}, timeout=30)  # English only here: the ages ("3mo ago") are read in English
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text)
        if not m:
            return {}
        out: dict[str, str] = {}

        def walk(x):
            if isinstance(x, dict):
                v = x.get("videoRenderer")
                if v and v.get("videoId") and (v.get("publishedTimeText") or {}).get("simpleText"):
                    out[v["videoId"]] = v["publishedTimeText"]["simpleText"]
                for y in x.values():
                    walk(y)
            elif isinstance(x, list):
                for y in x:
                    walk(y)
        walk(json.loads(m.group(1)))
        time.sleep(PAUSE / 2)
        return out
    except Exception:  # noqa: BLE001 - no ages then
        return {}


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
    out: list[dict] = []
    for w in windows:
        q = f'"{kw}"' + (f" after:{w[0].isoformat()} before:{w[1].isoformat()}" if w else "")
        r = _polite_get(f"https://news.google.com/rss/search?q={quote(q)}&hl=id&gl=ID&ceid=ID:id")
        if r is None:  # still refused after waiting: keep what was read, the next run goes on (2026-11-23)
            raise Partial(out, f"Google News membatasi permintaan (429) di \"{kw}\"; dilanjutkan di putaran berikutnya")
        for it in ET.fromstring(r.content).iter("item"):
            title, link, src = it.findtext("title"), it.findtext("link"), it.find("source")
            try:
                created = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                created = None
            out.append(_row("news", "google-news", kw, link, link, src.text if src is not None else None, title, created,
                            raw={"site": src.get("url") if src is not None else None}))
        time.sleep(NEWS_PAUSE_BACKFILL if since else PAUSE)
    return out


class Partial(Exception):
    """A source stopped early (a site asked to slow down): the rows read so far are kept."""

    def __init__(self, rows: list[dict], message: str):
        super().__init__(message)
        self.rows = rows


def _polite_get(url: str) -> Optional[requests.Response]:
    """GET; on 429 / 503 wait (60 s, then 120 s) and try again; None when still refused."""
    for wait in (0, 60, 120):
        if wait:
            time.sleep(wait)
        r = _http.get(url, timeout=30)
        if r.status_code not in (429, 503):
            r.raise_for_status()
            return r
    return None


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
                datetime.fromtimestamp(v["timestamp"], tz=timezone.utc) if v.get("timestamp") else _day(v.get("upload_date")),
                v.get("like_count"), v.get("comment_count"), v.get("view_count"), v.get("repost_count"),
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
        out: list[dict] = []
        for k in KEYWORDS:  # a keyword stopped early keeps what every keyword before it read
            try:
                out += news(k, since)
            except Partial as e:
                raise Partial(out + e.rows, str(e)) from None
        return out
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
                        created_at = CASE WHEN EXCLUDED.created_at IS NULL THEN {P}.social_post.created_at
                                          WHEN EXCLUDED.raw->>'date_approx' = 'true' AND {P}.social_post.raw->>'date_approx' IS NULL
                                               AND {P}.social_post.created_at IS NOT NULL THEN {P}.social_post.created_at
                                          ELSE EXCLUDED.created_at END,
                        raw = CASE WHEN EXCLUDED.raw->>'date_approx' IS NULL THEN {P}.social_post.raw - 'date_approx' - 'published_text'
                                   WHEN {P}.social_post.created_at IS NULL OR {P}.social_post.raw->>'date_approx' = 'true' THEN {P}.social_post.raw || EXCLUDED.raw
                                   ELSE {P}.social_post.raw END,
                        keywords = ARRAY(SELECT DISTINCT unnest({P}.social_post.keywords || EXCLUDED.keywords)),
                        text = coalesce(nullif(EXCLUDED.text, ''), {P}.social_post.text),  -- the title in its own language replaces a translated one
                        last_seen = now()
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
    except Partial as e:  # kept what was read; the backfill date stays, so the next run goes on
        found, added, err = len(e.rows), save(e.rows), str(e)[:500]
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

def posts(platform: Optional[str], q: str, sentiment_: Optional[str], d_from, d_to, limit: int, offset: int, keyword: Optional[str] = None,
          who: Optional[str] = None, kind: Optional[str] = None) -> dict:
    where = ["coalesce(created_at, first_seen) >= %(a)s", "coalesce(created_at, first_seen) < %(b)s + interval '1 day'"]
    p: dict = {"a": d_from, "b": d_to, "lim": limit, "off": offset}
    if platform:
        where.append("platform = %(pf)s"); p["pf"] = platform
    if sentiment_:
        where.append("sentiment = %(se)s"); p["se"] = sentiment_
    if q.strip():
        where.append("(text ILIKE %(q)s OR author ILIKE %(q)s OR transcript ILIKE %(q)s)"); p["q"] = f"%{q.strip()}%"
    if keyword:
        where.append("%(kw)s = ANY(keywords)"); p["kw"] = keyword
    if who:  # the source of the Overview's media tone (channel, else author, else platform)
        where.append("coalesce(nullif(raw->>'channel', ''), author, platform) = %(who)s"); p["who"] = who
    if kind == "post":  # the posts, not the comments under them
        where.append("source <> 'comment'")
    w = " AND ".join(where)
    rows = db.fetch(f"""SELECT platform, post_id, source, keyword, url, author, left(text, 600) AS text, created_at, likes, comments, keywords,
                               (raw->>'date_approx') = 'true' AS date_approx, raw->>'published_text' AS published_text,
                               views, shares, sentiment, first_seen FROM {P}.social_post WHERE {w}
                        ORDER BY coalesce(created_at, first_seen) DESC LIMIT %(lim)s OFFSET %(off)s""", p)
    t = db.fetchrow(f"""SELECT count(*)::int AS n, coalesce(sum(views), 0)::bigint AS views, coalesce(sum(likes), 0)::bigint AS likes,
                               coalesce(sum(comments), 0)::bigint AS comments,
                               count(*) FILTER (WHERE sentiment = 'positive')::int AS positive,
                               count(*) FILTER (WHERE sentiment = 'negative')::int AS negative
                          FROM {P}.social_post WHERE {w}""", p)
    total = t["n"]
    sums = {k: t[k] for k in ("views", "likes", "comments", "positive", "negative")} | {"neutral": total - t["positive"] - t["negative"]}
    return {"total": total, "sums": sums, "data": [{**r, "createdAt": r["created_at"].isoformat() if r["created_at"] else None, "dateApprox": bool(r["date_approx"]),
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
            "sentiment": r["sentiment"], "keywords": r["keywords"] or [], "raw": r["raw"] or {},
            "transcript": r.get("transcript"), "transcriptError": r.get("transcript_error"),
            "dateApprox": (r["raw"] or {}).get("date_approx") is True}



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


# ---------------------------------------------------------------- insights (the Overview, after a media-monitoring dashboard)

STOP = set("""yang dan di ke dari untuk dengan ini itu pada ada dalam juga tidak akan sudah bisa lebih kita kami saya aku kamu
anda dia mereka atau karena jadi oleh sebagai para serta agar namun hingga sampai saat setelah sebelum telah masih baru
banget aja sih nih yuk ya lagi buat bikin gak ga nggak udah kalo kalau tapi tp jd yg dgn utk dr sm aja deh kok dong loh lah
pun nya biar the a an of to in on for and is are with at by from this that it be as or you your we our my me i so all not
https http www com co id html amp quot rp ribu juta kopi calf kopicalf temancalf teman via kali satu dua tiga hari
semua sangat paling banyak cuma hanya mau ingin akan tentu jangan bukan begitu seperti sama setiap tahun bulan minggu ini
dalam sudah baca selengkapnya video channel subscribe follow like comment share""".split())
CITIES = ("Jakarta", "Bekasi", "Bogor", "Depok", "Tangerang", "Bandung", "Cirebon", "Tegal", "Pekalongan", "Semarang", "Solo",
          "Surakarta", "Yogyakarta", "Jogja", "Magelang", "Madiun", "Kediri", "Malang", "Surabaya", "Sidoarjo", "Mojokerto", "Jember",
          "Banyuwangi", "Denpasar", "Bali", "Lampung", "Palembang", "Jambi", "Padang", "Pekanbaru", "Medan", "Batam", "Aceh",
          "Pontianak", "Balikpapan", "Samarinda", "Banjarmasin", "Makassar", "Manado", "Kupang", "Mataram", "Serang", "Cilegon",
          "Karawang", "Purwokerto", "Cikarang", "Sukabumi", "Tasikmalaya", "Garut", "Kudus", "Gresik", "Probolinggo", "Lumajang")
_CITY = {c: re.compile(rf"\b{c}\b", re.I) for c in CITIES}
_ALIAS = {"Jogja": "Yogyakarta", "Solo": "Surakarta"}


def _words(texts: list[str], top: int = 60) -> list[dict]:
    freq: dict[str, int] = {}
    for t in texts:
        for w in set(re.findall(r"[a-zA-Z]{4,}", (t or "").lower())):
            if w not in STOP:
                freq[w] = freq.get(w, 0) + 1
    return [{"word": w, "n": n} for w, n in sorted(freq.items(), key=lambda x: -x[1])[:top] if n > 1]


def _cities(texts: list[str]) -> list[dict]:
    count: dict[str, int] = {}
    for t in texts:
        for c, rx in _CITY.items():
            if rx.search(t or ""):
                k = _ALIAS.get(c, c)
                count[k] = count.get(k, 0) + 1
    total = sum(count.values()) or 1
    return [{"city": c, "n": n, "share": round(n / total * 100, 2)} for c, n in sorted(count.items(), key=lambda x: -x[1])[:10]]


def insights(d_from, d_to) -> dict:
    """Everything the Overview shows: the sentiment against the period before, the channels, the tone per keyword and per
    source, the words, the cities named, keyword x week and the trending posts."""
    days = (d_to - d_from).days + 1
    p = {"a": d_from, "b": d_to, "pa": d_from - timedelta(days=days), "pb": d_from - timedelta(days=1)}
    rng = "coalesce(created_at, first_seen) >= %(a)s AND coalesce(created_at, first_seen) < %(b)s + interval '1 day'"
    prev = "coalesce(created_at, first_seen) >= %(pa)s AND coalesce(created_at, first_seen) < %(pb)s + interval '1 day'"
    feel = {r["s"]: r for r in db.fetch(f"""SELECT coalesce(sentiment, 'neutral') AS s, count(*) FILTER (WHERE {rng})::int AS now,
                                                   count(*) FILTER (WHERE {prev})::int AS before
                                              FROM {P}.social_post GROUP BY 1""", p)}
    channels = db.fetch(f"""SELECT platform, count(*)::int AS posts, coalesce(sum(views), 0)::bigint AS views FROM {P}.social_post
                            WHERE {rng} AND source <> 'comment' GROUP BY 1""", p)
    kw_tone = db.fetch(f"""SELECT k AS keyword, coalesce(sentiment, 'neutral') AS sentiment, count(*)::int AS n
                             FROM {P}.social_post, unnest(keywords) k WHERE {rng} GROUP BY 1, 2""", p)
    src = "coalesce(nullif(raw->>'channel', ''), author, platform)"
    media = db.fetch(f"""SELECT {src} AS source, max(platform) AS platform, count(*)::int AS n,
                                count(*) FILTER (WHERE sentiment = 'positive')::int AS positive,
                                count(*) FILTER (WHERE sentiment = 'negative')::int AS negative
                           FROM {P}.social_post WHERE {rng} AND source <> 'comment' AND source <> 'account'  -- outside voices, not our own account
                          GROUP BY 1 ORDER BY 3 DESC LIMIT 10""", p)
    texts = [r["text"] for r in db.fetch(f"SELECT text FROM {P}.social_post WHERE {rng} ORDER BY coalesce(created_at, first_seen) DESC LIMIT 3000", p)]
    week = "to_char(date_trunc('month', coalesce(created_at, first_seen) AT TIME ZONE 'Asia/Jakarta'), 'YYYY-MM-DD')"  # per month (2026-11-23)
    heat = db.fetch(f"""SELECT k AS keyword, {week} AS week, count(*)::int AS n FROM {P}.social_post, unnest(keywords) k
                         WHERE {rng} GROUP BY 1, 2 ORDER BY 2""", p)
    trending = db.fetch(f"""SELECT platform, post_id, source, author, url, left(text, 400) AS text, created_at, first_seen, (raw->>'date_approx') = 'true' AS date_approx, views, likes,
                                   comments, sentiment, keywords, raw->>'site' AS site
                              FROM {P}.social_post WHERE {rng} AND source <> 'comment'
                             ORDER BY created_at DESC NULLS LAST, coalesce(views, 0) DESC LIMIT 12""", p)  # the content date, never the day it was collected
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {
        "from": d_from.isoformat(), "to": d_to.isoformat(), "days": days,
        "sentiment": {k: {"now": (feel.get(k) or {}).get("now", 0), "before": (feel.get(k) or {}).get("before", 0)} for k in ("positive", "neutral", "negative")},
        "channels": channels, "keywordTone": kw_tone, "media": media, "words": _words(texts), "cities": _cities(texts),
        "heat": heat,
        "trending": [{**{k: v for k, v in r.items() if k not in ("created_at", "first_seen")}, "createdAt": iso(r["created_at"]), "dateApprox": bool(r["date_approx"])} for r in trending],
    }
