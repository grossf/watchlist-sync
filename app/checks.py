import logging
import threading

import httpx

from . import db, settings
from .letterboxd import Letterboxd, LetterboxdBlocked
from .seerr import Seerr

log = logging.getLogger("checks")

seerr_state: dict | None = None
seerr_users: list[dict] | None = None
watchlist_state: dict[str, dict] = {}
API_KEY_USER = 1


def describe_seerr_error(exc: Exception, url: str) -> str:
    if isinstance(exc, PermissionError):
        return str(exc)
    if isinstance(exc, httpx.ConnectError):
        return f"Could not connect to {url}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{url} did not answer in time"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{url} answered HTTP {exc.response.status_code}, is this really Seerr?"
    return str(exc) or exc.__class__.__name__


def test_seerr(url: str, api_key: str) -> dict:
    if not url or not api_key:
        return {"ok": False, "detail": "URL and API key are required", "checked_at": db.now()}
    try:
        client = Seerr(url, api_key)
        me = client.me()
        return {
            "ok": True,
            "user": me.get("displayName") or me.get("username") or me.get("email"),
            "version": client.version(),
            "checked_at": db.now(),
        }
    except Exception as exc:
        return {"ok": False, "detail": describe_seerr_error(exc, url), "checked_at": db.now()}


def test_watchlist(username: str, delay: float = 0) -> dict:
    try:
        info = Letterboxd(delay).watchlist_info(username)
        return {"ok": True, **info, "checked_at": db.now()}
    except LookupError:
        return {"ok": False, "missing": True, "checked_at": db.now(),
                "detail": f"Letterboxd has no public watchlist for {username}. "
                          "Check the spelling or make the watchlist public."}
    except LetterboxdBlocked:
        detail = "Letterboxd blocked the check. It's tried again before the next sync."
    except Exception:
        detail = "Couldn't reach Letterboxd. Check the server's internet connection."
    return {"ok": False, "detail": detail, "checked_at": db.now()}


def load_users(s: settings.Settings) -> list[dict]:
    global seerr_users
    seerr_users = Seerr(s.seerr_url, s.seerr_api_key).users()
    return seerr_users


def find_user(user_id: int | None) -> dict | None:
    user_id = user_id or API_KEY_USER
    return next((u for u in seerr_users or [] if u["id"] == user_id), None)


def refresh_seerr() -> None:
    global seerr_state, seerr_users
    s = settings.load()
    seerr_state = test_seerr(s.seerr_url, s.seerr_api_key) if s.seerr_url and s.seerr_api_key else None
    if not (seerr_state or {}).get("ok"):
        seerr_users = None
        return
    try:
        load_users(s)
    except Exception:
        log.exception("Could not load Seerr users")


def refresh() -> None:
    global watchlist_state
    refresh_seerr()
    s = settings.load()
    watchlist_state = {u: test_watchlist(u, s.letterboxd_delay) for u in s.letterboxd_usernames}


def refresh_in_background() -> None:
    threading.Thread(target=refresh, daemon=True, name="checks").start()


def refresh_seerr_in_background() -> None:
    threading.Thread(target=refresh_seerr, daemon=True, name="seerr-check").start()
