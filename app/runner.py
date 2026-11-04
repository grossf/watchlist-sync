import logging
import threading

from . import checks, db
from .config import Source
from .letterboxd import Film, Letterboxd, LetterboxdBlocked
from .seerr import REQUEST_PENDING, QuotaReached, Seerr, approval, existing_state
from .settings import Settings, load as load_settings

log = logging.getLogger("runner")

MAX_CONSECUTIVE_ERRORS = 5
MAX_PAGES = 180
RETRY_WAIT = 30

_lock = threading.Lock()
current: dict | None = None


class StopSync(Exception):
    pass


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def is_running() -> bool:
    return _lock.locked()


def _set_waiting(waiting: bool) -> None:
    if current is not None:
        current["waiting"] = waiting


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
    quota_hit = False
    states = db.film_states(source.key)
    seen: set[str] = set()
    user_name, user_approval = None, None

    def bump(key: str) -> None:
        counts[key] += 1
        current[key] = counts[key]

    def record(film: Film, kind: str, tmdb_id: int | None, detail: str | None = None, pending: bool | None = None) -> None:
        seen.add(film.slug)
        bump("requested" if kind == "would_request" else "errors" if kind == "error" else kind)
        if kind in ("in_seerr", "requested", "would_request"):
            detail = None
        prev = states.get(film.slug)
        if prev and prev["kind"] == kind and prev["detail"] == detail:
            return
        db.set_film_state(source.key, film.slug, film.name, tmdb_id, kind, detail)
        if kind == "in_seerr":
            return
        requesting = kind in ("requested", "would_request")
        db.add_event(run_id, kind, film.name, film.slug, tmdb_id, detail,
                     user_name if requesting else None, user_approval if requesting else None,
                     int(pending) if pending is not None else None)

    try:
        seerr_check = checks.seerr_state or {}
        if not seerr_check.get("ok"):
            raise StopSync(f"Seerr isn't reachable: {seerr_check.get('detail', 'not set up')}")
        user = checks.find_user(source.seerr_user_id)
        if checks.seerr_users is not None and user is None:
            raise StopSync(f"Seerr user #{source.seerr_user_id} doesn't exist anymore. "
                           "Pick another user for this watchlist in edit mode.")
        user_name = user["name"] if user else f"User #{source.seerr_user_id or checks.API_KEY_USER}"
        user_approval = approval(user.get("permissions"), s.request_4k) if user else None
        if user_approval == "none":
            raise StopSync(f"{user_name} isn't allowed to request {'4K ' if s.request_4k else ''}movies in Seerr. "
                           "Change their permissions in Seerr, or pick another user in edit mode.")
        try:
            for film in lb.iter_films(source.path, MAX_PAGES):
                counts["scanned"] += 1
                current["scanned"] = counts["scanned"]
                tmdb_id = None
                try:
                    tmdb_id, tmdb_type = _resolve_tmdb(lb, film)
                    if tmdb_type == "tv":
                        record(film, "skipped", tmdb_id, "Letterboxd links this to a TV show, only movies are requested")
                        continue

                    movie = seerr.movie(tmdb_id) if tmdb_id else None
                    if movie is None:
                        movie = seerr.search_movie(film.title, film.year)
                        if movie is None:
                            reason = (f"TMDB {tmdb_id} is unknown to Seerr" if tmdb_id
                                      else "Letterboxd has no TMDB ID for this film")
                            record(film, "not_found", tmdb_id, f"{reason}, and a title search found nothing")
                            continue
                        movie = seerr.movie(movie["id"]) or movie
                        tmdb_id = movie["id"]

                    state = existing_state(movie, s.request_4k)
                    if state:
                        record(film, state[0], tmdb_id, state[1])
                        continue

                    if dry_run:
                        record(film, "would_request", tmdb_id)
                    else:
                        created = seerr.request_movie(tmdb_id, s.request_4k, source.seerr_user_id)
                        record(film, "requested", tmdb_id, pending=created.get("status") == REQUEST_PENDING)
                    consecutive_errors = 0
                except (LetterboxdBlocked, QuotaReached):
                    raise
                except Exception as exc:
                    log.exception("Failed on %s", film.slug)
                    consecutive_errors += 1
                    last_error = str(exc)
                    record(film, "error", tmdb_id, last_error)
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise StopSync(f"Sync stopped after {consecutive_errors} errors in a row: {last_error}")
        except LetterboxdBlocked:
            raise StopSync(f"Sync stopped after {counts['scanned']} films: Letterboxd kept blocking requests, "
                           f"even after waiting {RETRY_WAIT} seconds. The next sync tries again.")
        except LookupError:
            checks.watchlist_state[source.username] = checks.test_watchlist(source.username)
            raise StopSync(f"Letterboxd has no public watchlist for {source.username}.")
        except QuotaReached:
            quota_hit = True
            counts["scanned"] -= 1
            current["scanned"] = counts["scanned"]
        if quota_hit:
            message = (f"{user_name} reached their Seerr request limit after {plural(counts['requested'], 'request')}. "
                       "The remaining films are tried at the next sync.")
            db.add_event(run_id, "limit_reached", detail=message, user_name=user_name, approval=user_approval)
        elif lb.truncated:
            message = f"Only the first {MAX_PAGES * 28} films were checked"
        elif counts["errors"]:
            message = f"{plural(counts['errors'], 'film')} failed: {last_error}"
        if not (lb.truncated or quota_hit):
            total = counts["scanned"]
            db.delete_film_states(source.key, [slug for slug in states if slug not in seen])
    except StopSync as exc:
        status, message = "failed", str(exc)
    except Exception as exc:
        log.exception("Sync for %s failed", source.key)
        status, message = "failed", f"Sync failed: {exc}"
    finally:
        if status == "failed":
            db.add_event(run_id, "sync_stopped", detail=message)
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
        lb = Letterboxd(s.letterboxd_delay, RETRY_WAIT, _set_waiting)
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
