"""Password hashing and session tokens (standard library only).

Passwords: scrypt (hashlib), stored as "scrypt$N$r$p$<salt b64>$<hash b64>".
Sessions: a random URL-safe token goes to the browser (HttpOnly cookie); the
database keeps only its SHA-256 hash, so a leaked table cannot be replayed.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
from typing import Optional

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN = 8


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    try:
        scheme, n, r, p, salt, digest = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), dklen=len(expected), maxmem=SCRYPT_MAXMEM)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# a fixed hash to compare against when the account does not exist, so the
# response time does not reveal whether a username/email is registered
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password: str) -> Optional[str]:
    """None when acceptable, else a message (Indonesian, shown in the UI)."""
    if len(password) < PASSWORD_MIN:
        return f"Password minimal {PASSWORD_MIN} karakter"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password harus mengandung huruf dan angka"
    if len(password) > 128:
        return "Password maksimal 128 karakter"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_password(length: int = 14) -> str:
    """Random password that satisfies password_problem()."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if password_problem(pw) is None:
            return pw
