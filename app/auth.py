import hashlib
import hmac
import secrets
import time

from . import settings

COOKIE = "watchlist_sync_session"
MAX_AGE = 30 * 86400


def enabled() -> bool:
    return bool(settings.secret("password_hash"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def check_password(password: str) -> bool:
    stored = settings.secret("password_hash")
    if not stored:
        return False
    salt, digest = stored.split(":")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


def set_password(password: str | None) -> None:
    settings.set_secret("password_hash", hash_password(password) if password else None)


def _key() -> bytes:
    secret = settings.secret("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        settings.set_secret("session_secret", secret)
    # Bound to the password hash so changing the password ends all sessions.
    return (secret + (settings.secret("password_hash") or "")).encode()


def make_token() -> str:
    payload = str(int(time.time()) + MAX_AGE)
    return f"{payload}.{hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()}"


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.split(".", 1)
    expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected) and payload.isdigit() and int(payload) > time.time()
