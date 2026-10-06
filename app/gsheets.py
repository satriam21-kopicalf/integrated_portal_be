"""Google Sheets delivery of export jobs (app/exports.py, format "gsheet").

The export is built as the usual ESB-layout .xlsx, then uploaded to Google Drive with
conversion to a Google Sheet (Drive keeps the sheets, dates and number formats), placed in
GOOGLE_DRIVE_FOLDER_ID and shared with the exporting user's email.

Credentials live in the server .env only, one of:
  - OAuth of a Google account (works with a personal Gmail): GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN and GOOGLE_DRIVE_FOLDER_ID, all
    printed by scripts/google_oauth_setup.py (scope drive.file: the portal only sees the
    files it creates).
  - a service account: GOOGLE_SERVICE_ACCOUNT_JSON_B64 (the key file, base64) and
    GOOGLE_DRIVE_FOLDER_ID of a folder on a Shared Drive the service account is a member of
    (service accounts have no Drive storage of their own).
"""
import base64
import json
import logging
import os
from typing import Callable, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Google Sheets holds at most 10 million cells per spreadsheet
MAX_CELLS = 10_000_000
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024  # resumable upload chunk, a multiple of 256 KiB
RETRIES = 5


class SheetsError(Exception):
    """A message for the user (Indonesian, shown in the export card)."""


def enabled() -> bool:
    s = get_settings()
    oauth = s.google_oauth_client_id and s.google_oauth_client_secret and s.google_oauth_refresh_token
    return bool(s.google_drive_folder_id and (oauth or s.google_service_account_json_b64))


def _session():
    from google.auth.transport.requests import AuthorizedSession  # local import: only export jobs need it

    s = get_settings()
    if s.google_service_account_json_b64:
        from google.oauth2 import service_account

        info = json.loads(base64.b64decode(s.google_service_account_json_b64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
    else:
        from google.oauth2.credentials import Credentials

        creds = Credentials(None, refresh_token=s.google_oauth_refresh_token, token_uri=TOKEN_URI,
                            client_id=s.google_oauth_client_id, client_secret=s.google_oauth_client_secret,
                            scopes=["https://www.googleapis.com/auth/drive.file"])
    return AuthorizedSession(creds)


def _fail(r, what: str):
    try:
        reason = r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        reason = r.text[:200]
    logger.error("Google Drive %s failed: HTTP %s %s", what, r.status_code, reason)
    raise SheetsError(f"Google Drive menolak {what} (HTTP {r.status_code}): {reason}")


def upload_as_sheet(path: str, title: str, share_with: Optional[str],
                    progress: Callable[[float], None] = lambda pct: None) -> dict:
    """Upload the .xlsx as a Google Sheet; returns {id, url, sharedWith}."""
    import requests

    s = get_settings()
    session = _session()
    size = os.path.getsize(path)
    params = {"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id,webViewLink"}
    r = session.post(DRIVE_UPLOAD, params=params, timeout=60,
                     json={"name": title, "mimeType": SHEET_MIME, "parents": [s.google_drive_folder_id]},
                     headers={"X-Upload-Content-Type": XLSX_MIME, "X-Upload-Content-Length": str(size)})
    if not r.ok:
        _fail(r, "upload")
    session_url = r.headers["Location"]

    offset, tries, result, error = 0, 0, None, None
    with open(path, "rb") as f:
        while result is None:
            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                # the last chunk also waits for the conversion to a Google Sheet
                r = session.put(session_url, data=chunk, timeout=1800,
                                headers={"Content-Range": f"bytes {offset}-{end}/{size}"})
            except requests.RequestException as exc:
                r, error = None, exc
            if r is not None and r.ok:
                result = r.json()
            elif r is not None and r.status_code == 308:  # chunk stored, more to send
                offset = int(r.headers["Range"].split("-")[1]) + 1 if "Range" in r.headers else 0
                tries = 0
                progress(offset / size)
            elif r is not None and r.status_code < 500 and r.status_code != 429:
                _fail(r, "upload")
            else:  # 5xx / 429 / network: ask Drive what it has and resume from there
                tries += 1
                if tries > RETRIES:
                    if r is not None:
                        _fail(r, "upload")
                    raise SheetsError(f"Upload ke Google Drive gagal: {error}")
                status = session.put(session_url, headers={"Content-Range": f"bytes */{size}"}, timeout=60)
                if status.ok:
                    result = status.json()
                elif status.status_code == 308:
                    offset = int(status.headers["Range"].split("-")[1]) + 1 if "Range" in status.headers else 0

    shared = _share(session, result["id"], share_with) if share_with else None
    return {"id": result["id"], "url": result.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{result['id']}",
            "sharedWith": shared}


def _share(session, file_id: str, email: str) -> Optional[str]:
    """Give the exporting user access; a failure leaves the sheet in the folder only."""
    url = f"{DRIVE}/files/{file_id}/permissions"
    body = {"type": "user", "role": get_settings().google_share_role, "emailAddress": email}
    # without a notification first; an address that is not a Google account needs the e-mail invite
    for notify in ("false", "true"):
        r = session.post(url, params={"supportsAllDrives": "true", "sendNotificationEmail": notify}, json=body, timeout=60)
        if r.ok:
            return email
    logger.warning("Google Sheet %s not shared with %s: HTTP %s %s", file_id, email, r.status_code, r.text[:200])
    return None
