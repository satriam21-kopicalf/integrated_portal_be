"""Face detection of the PIC Check In selfie (app/pic_checkin.py), on the server so the browser cannot skip it.

OpenCV's frontal-face Haar cascade on a grey, histogram-equalised copy at most 800 px wide. A face must be at least 12%
of the shorter side of the photo: a selfie, not a face far away in the background. ~20-30 ms per photo.
"""
from typing import Optional

MAX_SIDE = 800
MIN_FACE = 0.12

_cascade = None


def _detector():
    global _cascade
    if _cascade is None:
        import cv2  # imported on first use: keeps the app start light

        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _cascade


def count_faces(data: bytes) -> Optional[int]:
    """Number of faces in a JPEG/PNG/WebP image; None when the image cannot be read."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, MAX_SIDE / max(h, w))
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    img = cv2.equalizeHist(img)
    side = int(min(img.shape[:2]) * MIN_FACE)
    faces = _detector().detectMultiScale(img, scaleFactor=1.1, minNeighbors=5, minSize=(side, side))
    return len(faces)
