"""Small copies of the stored photos for lists and thumbnails (?w=<px> on the photo routes).

The forms store photos up to 1600 px (~300 KB); a list showing them at 96 px would download every one in full. With
`w` the route answers a JPEG at most `w` px wide (never larger than the original), made with OpenCV (already a
dependency) and kept in a small in-process cache keyed by the content, so a photo that changes never shows a stale copy.
Without `w`, or for anything that is not a JPEG / PNG / WebP image, the original is returned as before.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Optional

import cv2
import numpy as np
from fastapi.responses import Response

WIDTHS = (96, 160, 240, 320, 480, 640, 960)  # `w` snaps up to one of these (bounded cache, shared copies)
_IMAGES = {"image/jpeg", "image/png", "image/webp"}
_BUDGET = 48 * 1024 * 1024  # bytes of thumbnails kept in memory
_cache: OrderedDict[tuple[bytes, int], bytes] = OrderedDict()
_size = 0
_lock = threading.Lock()


def _snap(w: int) -> int:
    return next((x for x in WIDTHS if x >= w), WIDTHS[-1])


def _make(data: bytes, w: int) -> Optional[bytes]:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h0, w0 = img.shape[:2]
    if w0 > w:
        img = cv2.resize(img, (w, max(1, round(h0 * w / w0))), interpolation=cv2.INTER_AREA)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 74, cv2.IMWRITE_JPEG_OPTIMIZE, 1])
    return out.tobytes() if ok else None


def thumbnail(data: bytes, mime: Optional[str], w: Optional[int]) -> tuple[bytes, Optional[str]]:
    """(bytes, media type) of the photo at most `w` px wide, or the original when `w` is None / not an image."""
    global _size
    if not w or (mime or "").lower() not in _IMAGES:
        return data, mime
    w = _snap(w)
    key = (hashlib.blake2b(data, digest_size=16).digest(), w)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit, "image/jpeg"
    small = _make(data, w)
    if small is None or len(small) >= len(data):
        return data, mime
    with _lock:
        if key not in _cache:
            _cache[key] = small
            _size += len(small)
            while _size > _BUDGET and _cache:
                _, old = _cache.popitem(last=False)
                _size -= len(old)
    return small, "image/jpeg"


def image_response(data: bytes, mime: Optional[str], w: Optional[int], cache: str, **headers: str) -> Response:
    """The photo (or its small copy) with the given Cache-Control."""
    body, kind = thumbnail(bytes(data), mime, w)
    return Response(body, media_type=kind, headers={"Cache-Control": cache, **headers})
