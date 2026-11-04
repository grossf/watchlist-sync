import logging
import threading

import httpx

from . import db, settings
from .letterboxd import Letterboxd, LetterboxdBlocked
from .seerr import Seerr

log = logging.getLogger("checks")

seerr_state: dict | None = None
watchlist_state: dict[str, dict] = {}


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


def refresh() -> None:
    global seerr_state, watchlist_state
    s = settings.load()
    seerr_state = test_seerr(s.seerr_url, s.seerr_api_key) if s.seerr_url and s.seerr_api_key else None
    watchlist_state = {u: test_watchlist(u, s.letterboxd_delay) for u in s.letterboxd_usernames}


def refresh_in_background() -> None:
    threading.Thread(target=refresh, daemon=True, name="checks").start()


def refresh_seerr_in_background() -> None:
    def work() -> None:
        global seerr_state
        s = settings.load()
        seerr_state = test_seerr(s.seerr_url, s.seerr_api_key) if s.seerr_url and s.seerr_api_key else None
    threading.Thread(target=work, daemon=True, name="seerr-check").start()
