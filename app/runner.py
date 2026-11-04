import logging
import threading

from . import checks, db
from .config import Source
from .letterboxd import Film, Letterboxd, LetterboxdBlocked
from .seerr import Seerr, existing_state
from .settings import Settings, load as load_settings

log = logging.getLogger("runner")

MAX_CONSECUTIVE_ERRORS = 5
MAX_PAGES = 180

_lock = threading.Lock()
current: dict | None = None


class StopSync(Exception):
    pass


def is_running() -> bool:
    return _lock.locked()


def _resolve_tmdb(lb: Letterboxd, film: Film) -> tuple[int | None, str | None]:
    cached = db.cached_film(film.slug)
    if cached:
        return cached["tmdb_id"], cached["tmdb_type"]
    tmdb_id, tmdb_type = lb.tmdb(film.slug)
    db.cache_film(film.slug, tmdb_id, tmdb_type)
    return tmdb_id, tmdb_type


def _run_source(s: Settings, source: Source, trigger: str, dry_run: bool, lb: Letterboxd, seerr: Seerr) -> None:
    global current
    run_id = db.start_run(source.key, source.label, trigger, dry_run)
    total = (checks.watchlist_state.get(source.username) or {}).get("count")
    counts = {"scanned": 0, "in_seerr": 0, "requested": 0, "skipped": 0, "not_found": 0, "errors": 0}
    current = {"run_id": run_id, "source": source.key, "username": source.username,
               "dry_run": dry_run, "total": total, **counts}
    status, message = "ok", None
    consecutive_errors = 0
    last_error = None

    def bump(key: str) -> None:
        counts[key] += 1
        current[key] = counts[key]

    try:
        seerr_check = checks.seerr_state or {}
        if not seerr_check.get("ok"):
            raise StopSync(f"Seerr isn't reachable: {seerr_check.get('detail', 'not set up')}")
        try:
            for film in lb.iter_films(source.path, MAX_PAGES):
                bump("scanned")
                tmdb_id = None
                try:
                    tmdb_id, tmdb_type = _resolve_tmdb(lb, film)
                    if tmdb_type == "tv":
                        bump("skipped")
                        db.add_event(run_id, "skipped", film.name, film.slug, tmdb_id,
                                     "Letterboxd links this to a TV show, only movies are requested")
                        continue

                    movie = seerr.movie(tmdb_id) if tmdb_id else None
                    if movie is None:
                        movie = seerr.search_movie(film.title, film.year)
                        if movie is None:
                            bump("not_found")
                            reason = (f"TMDB {tmdb_id} is unknown to Seerr" if tmdb_id
                                      else "Letterboxd has no TMDB id for this film")
                            db.add_event(run_id, "not_found", film.name, film.slug, tmdb_id,
                                         f"{reason}, and a title search found nothing")
                            continue
                        movie = seerr.movie(movie["id"]) or movie
                        tmdb_id = movie["id"]

                    state = existing_state(movie, s.request_4k)
                    if state:
                        kind, reason = state
                        bump(kind)
                        if kind == "skipped":
                            db.add_event(run_id, "skipped", film.name, film.slug, tmdb_id, reason)
                        continue

                    if dry_run:
                        db.add_event(run_id, "would_request", film.name, film.slug, tmdb_id,
                                     "Dry run, nothing was sent")
                    else:
                        seerr.request_movie(tmdb_id, s.request_4k, s.seerr_user_id)
                        db.add_event(run_id, "requested", film.name, film.slug, tmdb_id)
                    bump("requested")
                    consecutive_errors = 0
                except LetterboxdBlocked:
                    raise
                except Exception as exc:
                    log.exception("Failed on %s", film.slug)
                    bump("errors")
                    consecutive_errors += 1
                    last_error = str(exc)
                    db.add_event(run_id, "error", film.name, film.slug, tmdb_id, last_error)
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise StopSync(f"Sync stopped after {consecutive_errors} errors in a row: {last_error}")
        except LetterboxdBlocked:
            raise StopSync(f"Sync stopped after {counts['scanned']} films: Letterboxd blocked the request. "
                           "The next sync tries again.")
        except LookupError:
            checks.watchlist_state[source.username] = checks.test_watchlist(source.username)
            raise StopSync(f"Letterboxd has no public watchlist for {source.username}.")
        if lb.truncated:
            message = f"Only the first {MAX_PAGES * 28} films were checked"
        elif counts["errors"]:
            failed = counts["errors"]
            message = f"{failed} film{'' if failed == 1 else 's'} failed: {last_error}"
        if not lb.truncated:
            total = counts["scanned"]
    except StopSync as exc:
        status, message = "failed", str(exc)
    except Exception as exc:
        log.exception("Sync for %s failed", source.key)
        status, message = "failed", f"Sync failed: {exc}"
    finally:
        db.update_run(run_id, status=status, message=message, finished_at=db.now(), total=total, **counts)
        log.info("%s finished: %s %s", source.label, status, counts)


def run(source_keys: list[str] | None, trigger: str, dry_run: bool | None = None) -> bool:
    global current
    if not _lock.acquire(blocking=False):
        return False
    try:
        s = load_settings()
        checks.refresh()
        dry = s.dry_run if dry_run is None else dry_run
        sources = [src for src in s.sources if source_keys is None or src.key in source_keys]
        lb = Letterboxd(s.letterboxd_delay)
        seerr = Seerr(s.seerr_url, s.seerr_api_key)
        for source in sources:
            _run_source(s, source, trigger, dry, lb, seerr)
        db.prune(s.log_retention_days)
    finally:
        current = None
        _lock.release()
    return True


def run_in_background(source_keys: list[str] | None, trigger: str, dry_run: bool | None = None) -> bool:
    if is_running():
        return False
    threading.Thread(target=run, args=(source_keys, trigger, dry_run), daemon=True).start()
    return True
