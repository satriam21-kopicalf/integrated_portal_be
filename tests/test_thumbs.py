"""Small copies of the photos (?w= on the photo routes, app/thumbs.py)."""
import cv2
import numpy as np

from app import thumbs


def _jpeg(w: int, h: int) -> bytes:
    img = np.zeros((h, w, 3), np.uint8)
    img[:, : w // 2] = (40, 80, 200)
    cv2.putText(img, "TEST", (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 4, (255, 255, 255), 8)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def test_thumbnail_is_smaller_and_snapped():
    data = _jpeg(1600, 1200)
    small, kind = thumbs.thumbnail(data, "image/jpeg", 150)
    assert kind == "image/jpeg" and len(small) < len(data)
    img = cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == 160 and img.shape[0] == 120  # snapped up to 160 px, ratio kept


def test_original_without_w_or_for_other_files():
    data = _jpeg(800, 600)
    assert thumbs.thumbnail(data, "image/jpeg", None) == (data, "image/jpeg")
    assert thumbs.thumbnail(b"%PDF-1.4", "application/pdf", 160) == (b"%PDF-1.4", "application/pdf")
    assert thumbs.thumbnail(b"not an image", "image/jpeg", 160) == (b"not an image", "image/jpeg")


def test_cached_copy_is_reused():
    data = _jpeg(1200, 900)
    a, _ = thumbs.thumbnail(data, "image/jpeg", 240)
    b, _ = thumbs.thumbnail(data, "image/jpeg", 240)
    assert a is b


def test_never_upscales_a_small_photo():
    data = _jpeg(120, 90)
    small, _ = thumbs.thumbnail(data, "image/jpeg", 640)
    img = cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] <= 120
