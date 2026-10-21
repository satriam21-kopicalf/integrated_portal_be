"""Google Sheets delivery of export jobs (app/exports.py, format "gsheet").

The export is built as the usual ESB-layout .xlsx, then handed to an Apps Script web app in the Google account that
owns the files (scripts/google_apps_script.gs, no Google Cloud project needed): it converts the file to a Google Sheet
(Drive keeps the sheets, dates and number formats), keeps it in its own folder and shares it with the exporting user's
email. Credentials live in the server .env only: GOOGLE_APPS_SCRIPT_URL + GOOGLE_APPS_SCRIPT_KEY (random, made on the
server); the script accepts only the key it was paired with (pair() on first contact). Apps Script takes requests up
to ~50 MB and 6 minutes, so the .xlsx may be at most APPS_SCRIPT_MAX_BYTES.
"""
import base64
import logging
import os
from typing import Callable, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# base64 adds a third: ~48 MB request, under the Apps Script web app limit
APPS_SCRIPT_MAX_BYTES = 36 * 1024 * 1024


class SheetsError(Exception):
    """A message for the user (Indonesian, shown in the export card)."""


def enabled() -> bool:
    s = get_settings()
    return bool(s.google_apps_script_url and s.google_apps_script_key)


def upload_as_sheet(path: str, title: str, share_with: Optional[str],
                    progress: Callable[[float], None] = lambda pct: None, folder: Optional[str] = None) -> dict:
    """Upload the .xlsx as a Google Sheet, shared with `share_with` and (GOOGLE_SHARE_LINK) with anyone who has the
    link; `folder` = a sub-folder for the parts of one export. Returns {id, url, sharedWith, linkAccess, folderUrl}."""
    size = os.path.getsize(path)
    if size > APPS_SCRIPT_MAX_BYTES:
        raise SheetsError(f"File export {size / 1048576:.0f} MB melebihi batas Google Apps Script "
                          f"({APPS_SCRIPT_MAX_BYTES // 1048576} MB). Perkecil periode atau cabang, atau export ke Excel.")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    progress(0.1)
    # the conversion runs inside the call (Apps Script stops at 6 minutes)
    s = get_settings()
    out = _apps_script_call({"action": "upload", "name": title, "data": data, "shareWith": share_with,
                             "role": s.google_share_role, "folder": folder, "linkAccess": s.google_share_link},
                            timeout=420)
    # an older script ignores folder / linkAccess: it answers without "linkAccess"
    return {"id": out["id"], "url": out["url"], "sharedWith": out.get("sharedWith"),
            "linkAccess": out.get("linkAccess"), "folderUrl": out.get("folderUrl")}


def _apps_script_call(body: dict, timeout: int) -> dict:
    """POST to the Apps Script web app (it answers through a redirect to the result)."""
    import requests

    s = get_settings()
    try:
        r = requests.post(s.google_apps_script_url, json={**body, "key": s.google_apps_script_key}, timeout=timeout)
    except requests.RequestException as exc:
        raise SheetsError(f"Google Apps Script tidak dapat dihubungi: {exc}") from exc
    try:
        out = r.json()
    except ValueError:
        logger.error("Apps Script answered HTTP %s, not JSON: %s", r.status_code, r.text[:200])
        raise SheetsError(f"Google Apps Script menjawab HTTP {r.status_code} (bukan JSON): periksa deployment web app "
                          "(Execute as: Me, Who has access: Anyone)") from None
    if not out.get("ok"):
        logger.error("Apps Script refused: %s", out.get("error"))
        raise SheetsError(f"Google Apps Script menolak: {out.get('error')}")
    return out


def cleanup(days: int) -> int:
    """Move Google Sheets exports older than `days` to the Drive trash; returns how many."""
    return int(_apps_script_call({"action": "cleanup", "days": days}, timeout=360).get("trashed", 0))


def pair() -> dict:
    """Register this server's key with the Apps Script (first contact wins; idempotent). Run once on the server after
    setting the key: docker exec integrated-portal-be python -c "from app import gsheets; print(gsheets.pair())" """
    return _apps_script_call({"action": "pair"}, timeout=120)
