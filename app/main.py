import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, checks, db, runner, scheduler, settings
from .config import GITHUB_URL, TZ, VERSION
from .seerr import Seerr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
mimetypes.add_type("font/woff2", ".woff2")
OPEN_PATHS = {"/healthz", "/login", "/api/login"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    scheduler.start()
    checks.refresh_in_background()
    yield


app = FastAPI(title="watchlist-sync", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/") or not auth.enabled() or auth.valid(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return RedirectResponse("/login")


def _session_response(body: dict, request: Request) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(auth.COOKIE, auth.make_token(), max_age=auth.MAX_AGE, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https")
    return resp


class LoginBody(BaseModel):
    password: str


class RunBody(BaseModel):
    source: str = "all"
    dry_run: bool | None = None


class SeerrTestBody(BaseModel):
    seerr_url: str | None = None
    seerr_api_key: str | None = None


class WatchlistBody(BaseModel):
    username: str


class OrderBody(BaseModel):
    usernames: list[str]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    if not auth.enabled():
        return RedirectResponse("/")
    return FileResponse(STATIC / "login.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    if not auth.check_password(body.password):
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    return _session_response({"ok": True}, request)


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


def _watchlist_key(username: str) -> str:
    return f"watchlist:{username}"


@app.get("/api/status")
def status():
    s = settings.load()
    last = db.last_run_per_source()
    seerr_check = checks.seerr_state
    return {
        "version": VERSION,
        "github_url": GITHUB_URL,
        "running": runner.is_running(),
        "current": runner.current,
        "next_run": scheduler.next_run.isoformat() if scheduler.next_run else None,
        "schedule_enabled": s.schedule_enabled,
        "schedule_time": s.schedule_time,
        "timezone": str(TZ),
        "dry_run": s.dry_run,
        "password_enabled": auth.enabled(),
        "seerr": {
            "configured": bool(s.seerr_url and s.seerr_api_key),
            "url": s.seerr_url,
            "check": seerr_check,
        },
        "watchlists": [
            {"username": u, "key": _watchlist_key(u), "url": f"https://letterboxd.com/{u}/watchlist/",
             "check": checks.watchlist_state.get(u), "last_run": last.get(_watchlist_key(u))}
            for u in s.letterboxd_usernames
        ],
    }


@app.post("/api/run")
def run(body: RunBody):
    s = settings.load()
    keys = None if body.source == "all" else [body.source]
    if keys and keys[0] not in {src.key for src in s.sources}:
        raise HTTPException(404, "Unknown watchlist")
    if not (s.seerr_url and s.seerr_api_key):
        raise HTTPException(400, "Connect Seerr first")
    if not s.letterboxd_usernames:
        raise HTTPException(400, "Add a watchlist first")
    if not runner.run_in_background(keys, "manual", body.dry_run):
        raise HTTPException(409, "A sync is already running")
    return {"started": True}


@app.get("/api/runs")
def runs(watchlist: str | None = None, limit: int = 30):
    return db.runs(_watchlist_key(watchlist) if watchlist else None, min(limit, 200))


@app.get("/api/events")
def events(kind: str | None = None, watchlist: str | None = None, limit: int = 300):
    kinds = {"requested": ["requested", "would_request"]}.get(kind, [kind]) if kind else None
    return db.events(kinds, _watchlist_key(watchlist) if watchlist else None, limit=min(limit, 2000))


def _check_username(raw: str) -> tuple[str, dict]:
    try:
        username = settings.normalize_username(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return username, checks.test_watchlist(username)


@app.post("/api/test/watchlist")
def test_watchlist(body: WatchlistBody):
    username, result = _check_username(body.username)
    already = username in settings.load().letterboxd_usernames
    return {"username": username, "already_added": already, **result}


@app.post("/api/watchlists")
def add_watchlist(body: WatchlistBody):
    username, result = _check_username(body.username)
    s = settings.load()
    if username in s.letterboxd_usernames:
        raise HTTPException(409, f"{username} is already in your list")
    if not result["ok"]:
        raise HTTPException(400, result["detail"])
    settings.save({"letterboxd_usernames": s.letterboxd_usernames + [username]})
    checks.watchlist_state = {**checks.watchlist_state, username: result}
    return {"username": username, **result}


@app.delete("/api/watchlists/{username}")
def remove_watchlist(username: str):
    s = settings.load()
    if username not in s.letterboxd_usernames:
        raise HTTPException(404, "Unknown watchlist")
    settings.save({"letterboxd_usernames": [u for u in s.letterboxd_usernames if u != username]})
    checks.watchlist_state = {u: c for u, c in checks.watchlist_state.items() if u != username}
    return {"ok": True}


@app.put("/api/watchlists/order")
def reorder_watchlists(body: OrderBody):
    s = settings.load()
    if sorted(body.usernames) != sorted(s.letterboxd_usernames):
        raise HTTPException(409, "The watchlists changed in the meantime, reload the page")
    settings.save({"letterboxd_usernames": body.usernames})
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return settings.public(settings.load())


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    password = body.pop("password", None)
    clear_password = body.pop("clear_password", False)
    body.pop("letterboxd_usernames", None)
    if password and len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    try:
        s = settings.save(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if clear_password:
        auth.set_password(None)
    elif password:
        auth.set_password(password)
    scheduler.reschedule()
    if any(k.startswith("seerr_") for k in body):
        checks.seerr_state = None
        checks.refresh_seerr_in_background()
    result = settings.public(s)
    result["password_enabled"] = auth.enabled()
    return _session_response(result, request) if password and not clear_password else result


def _seerr_credentials(body: SeerrTestBody) -> tuple[str, str]:
    s = settings.load()
    url = settings.validate({"seerr_url": body.seerr_url})["seerr_url"] if body.seerr_url else s.seerr_url
    key = (body.seerr_api_key or "").strip()
    if not key:
        if url != s.seerr_url:
            raise HTTPException(400, "Enter the API key to test a different Seerr URL")
        key = s.seerr_api_key
    return url, key


@app.post("/api/test/seerr")
def test_seerr(body: SeerrTestBody):
    try:
        url, key = _seerr_credentials(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return checks.test_seerr(url, key)


@app.post("/api/seerr/users")
def seerr_users(body: SeerrTestBody):
    try:
        url, key = _seerr_credentials(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        return Seerr(url, key).users()
    except Exception as exc:
        raise HTTPException(502, f"Could not load Seerr users: {checks.describe_seerr_error(exc, url)}")


@app.post("/api/checks/refresh")
def refresh_checks():
    checks.refresh()
    return status()
