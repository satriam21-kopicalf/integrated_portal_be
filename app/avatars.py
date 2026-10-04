"""Profile pictures: validation and storage (integration_portal.user_avatar).

Uploads arrive as a data URL ("data:image/webp;base64,...") already cropped and
resized by the browser. Only WebP, JPEG and PNG up to 512 KB are accepted, and
the bytes must carry the matching file signature (the declared type alone is
not trusted).
"""
import base64
import binascii
import re
from typing import Optional

from app import database as db

MAX_BYTES = 512 * 1024
DATA_URL_RE = re.compile(r"^data:(image/(?:webp|jpeg|png));base64,([A-Za-z0-9+/=\s]+)$")
T_AVATAR = "integration_portal.user_avatar"
T_USER = "integration_portal.user_account"


def _signature_ok(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def parse_data_url(value: str) -> tuple[Optional[tuple[str, bytes]], Optional[str]]:
    """((content_type, bytes), None) or (None, error message)."""
    match = DATA_URL_RE.match((value or "").strip())
    if not match:
        return None, "Format gambar tidak didukung (gunakan JPG, PNG atau WebP)"
    content_type = match.group(1)
    try:
        data = base64.b64decode(re.sub(r"\s", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        return None, "Data gambar rusak"
    if len(data) > MAX_BYTES:
        return None, "Ukuran gambar maksimal 512 KB"
    if not _signature_ok(content_type, data):
        return None, "File bukan gambar yang valid"
    return (content_type, data), None


def save(user_id: str, content_type: str, data: bytes, actor_id: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(
            f"""INSERT INTO {T_AVATAR} (user_id, content_type, data, updated_at) VALUES (%s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET content_type = EXCLUDED.content_type, data = EXCLUDED.data,
                    updated_at = now()""",
            (user_id, content_type, data))
        conn.execute(f"UPDATE {T_USER} SET avatar_updated_at = now(), updated_at = now(), updated_by = %s WHERE id = %s",
                     (actor_id, user_id))


def remove(user_id: str, actor_id: Optional[str]) -> None:
    with db.transaction() as conn:
        conn.execute(f"DELETE FROM {T_AVATAR} WHERE user_id = %s", (user_id,))
        conn.execute(f"UPDATE {T_USER} SET avatar_updated_at = NULL, updated_at = now(), updated_by = %s WHERE id = %s",
                     (actor_id, user_id))


def load(user_id: str) -> Optional[dict]:
    return db.fetchrow(f"SELECT content_type, data, updated_at FROM {T_AVATAR} WHERE user_id = %s", (user_id,))
