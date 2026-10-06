"""One-time setup of the Google Sheets export (app/gsheets.py) with a Google account.

Run it on your own computer (it opens the browser for the Google sign-in):

    python scripts/google_oauth_setup.py path/to/client_secret.json ["Folder name"]

client_secret.json is the OAuth client (type "Desktop app") downloaded from Google Cloud
Console (APIs & Services > Credentials), in a project with the Google Drive API enabled and
the OAuth consent screen published ("In production"; in "Testing" Google expires the
refresh token after 7 days). The script signs in with scope drive.file (the portal only sees
the files it creates), creates the Drive folder the exports go to, and prints the lines for
the server .env (/opt/integrated-portal-be/.env). Keep them secret; never commit them.
Standard library only.
"""
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from base64 import urlsafe_b64encode

SCOPE = "https://www.googleapis.com/auth/drive.file"


def _post(url: str, data: dict, token: str = None, as_json: bool = False) -> dict:
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json" if as_json else "application/x-www-form-urlencoded")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read())


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    client = json.load(open(sys.argv[1], encoding="utf-8"))
    client = client.get("installed") or client.get("web") or client
    folder_name = sys.argv[2] if len(sys.argv) > 2 else "Kopi Calf Portal Exports"

    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            result.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Selesai, kembali ke terminal. / Done, return to the terminal.".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    })
    print("Opening the browser for the Google sign-in. If it does not open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("state", [None])[0] != state or "code" not in result:
        sys.exit(f"Sign-in failed: {result.get('error')}")

    tokens = _post("https://oauth2.googleapis.com/token", {
        "code": result["code"][0], "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if "refresh_token" not in tokens:
        sys.exit("Google returned no refresh token; remove the app's access at myaccount.google.com/permissions and run again.")
    folder = _post("https://www.googleapis.com/drive/v3/files?fields=id,webViewLink",
                   {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
                   token=tokens["access_token"], as_json=True)

    print(f"\nDrive folder created: {folder.get('webViewLink')}")
    print("Exports land there and are shared with the exporting user's e-mail; share the folder itself with")
    print("anyone who should see every export.\n\nAdd to /opt/integrated-portal-be/.env, then restart the container:\n")
    print(f"GOOGLE_DRIVE_FOLDER_ID={folder['id']}")
    print(f"GOOGLE_OAUTH_CLIENT_ID={client['client_id']}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={client['client_secret']}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={tokens['refresh_token']}")


if __name__ == "__main__":
    main()
