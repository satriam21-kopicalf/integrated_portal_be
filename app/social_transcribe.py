"""Speech to text for Social Media Analytics (migration 138, 2026-11-24): the audio of every TikTok video we keep,
transcribed on our VPS by a local Whisper model (faster-whisper, CPU, int8). No third-party service: the open model is
fetched once into /data/media/whisper and kept across deploys.

A background thread takes the newest videos without a transcript a few at a time, downloads only their audio into a
temporary folder (yt-dlp, deleted afterwards), transcribes it in Indonesian and stores the text; the keyword labels and
the sentiment are then worked out again from the caption and the speech together. A video whose audio cannot be read
keeps the reason (transcript_error) and is not tried again.
"""
import logging
import os
import tempfile
import threading
import time
from typing import Optional

from app import database as db
from app import social

logger = logging.getLogger(__name__)
P = social.P
MODEL = os.getenv("WHISPER_MODEL", "small")
MODEL_DIR = os.getenv("WHISPER_DIR", "/data/media/whisper")
THREADS = int(os.getenv("WHISPER_THREADS", "4"))   # half the VPS, the portal keeps the rest
BATCH = 5
MAX_S = 600                                         # longer videos are skipped (10 minutes)
IDLE_S = 600

_model = None
_lock = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS, download_root=MODEL_DIR)
    return _model


def transcribe_url(url: str) -> str:
    """The speech of one video: its audio only, downloaded, transcribed, deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        with social._ydl({"format": "bestaudio/best", "outtmpl": os.path.join(tmp, "a.%(ext)s"),
                          "skip_download": False, "match_filter": lambda i, *_: "too long" if (i.get("duration") or 0) > MAX_S else None}) as y:
            y.download([url])
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
        if not files:
            raise RuntimeError("no audio (too long or refused)")
        segs, _ = _whisper().transcribe(files[0], language="id", vad_filter=True, beam_size=1)
        return " ".join(s.text.strip() for s in segs).strip()


def pending(limit: int = BATCH) -> list[dict]:
    return db.fetch(f"""SELECT platform, post_id, url, text, author, keyword FROM {P}.social_post
                        WHERE platform = 'tiktok' AND source <> 'comment' AND transcribed_at IS NULL AND url IS NOT NULL
                        ORDER BY created_at DESC NULLS LAST LIMIT %s""", (limit,))


def run_batch(limit: int = BATCH) -> int:
    done = 0
    for r in pending(limit):
        text: Optional[str] = None
        err: Optional[str] = None
        try:
            text = transcribe_url(r["url"])
        except Exception as e:  # noqa: BLE001 - kept as the reason, the video is not tried again
            err = str(e)[:300]
            logger.info("transcribe %s: %s", r["post_id"], err)
        both = " ".join(x for x in (r["text"], text) if x)
        kws = social.labels(both, r["author"]) or ([r["keyword"]] if r["keyword"] in social.LABELS else [])
        with db.transaction() as conn:
            conn.execute(f"""UPDATE {P}.social_post SET transcript = %s, transcript_error = %s, transcribed_at = now(),
                                    sentiment = %s, keywords = ARRAY(SELECT DISTINCT unnest(keywords || %s::text[]))
                             WHERE platform = %s AND post_id = %s""",
                         (text or None, err, social.sentiment(both), kws, r["platform"], r["post_id"]))
        done += 1
        time.sleep(social.PAUSE)
    return done


def _loop() -> None:
    time.sleep(social.FIRST_S + 60)
    while True:
        try:
            n = run_batch()
        except Exception as e:  # noqa: BLE001 - the loop never dies
            logger.warning("transcribe: %s", e)
            n = 0
        time.sleep(30 if n else IDLE_S)


def start() -> None:
    """One transcriber for the four uvicorn workers: the one that gets the lock file runs it, the others never do."""
    import fcntl
    global _lock
    try:
        os.makedirs(MODEL_DIR, exist_ok=True)
        _lock = open(os.path.join(MODEL_DIR, ".lock"), "w")
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    threading.Thread(target=_loop, name="social-transcribe", daemon=True).start()
