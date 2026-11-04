import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://letterboxd.com"
USER_AGENT = "watchlist-sync/1.0 (self-hosted)"
NAME_YEAR = re.compile(r"^(.*)\s\((\d{4})\)$")


class LetterboxdBlocked(Exception):
    pass


@dataclass
class Film:
    slug: str
    name: str
    title: str
    year: int | None


def _blocked(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    return resp.status_code in (403, 503) and ("Just a moment" in resp.text or "challenge" in resp.text.lower())


class Letterboxd:
    def __init__(self, delay: float, retry_wait: float = 0, on_wait: Callable[[bool], None] | None = None) -> None:
        self.delay = delay
        self.retry_wait = retry_wait
        self.on_wait = on_wait
        self._last = 0.0
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=30,
            follow_redirects=True,
        )

    def _request(self, path: str) -> httpx.Response:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = self.client.get(path)
        self._last = time.monotonic()
        return resp

    def _get(self, path: str) -> str:
        resp = self._request(path)
        if _blocked(resp) and self.retry_wait:
            if self.on_wait:
                self.on_wait(True)
            time.sleep(self.retry_wait)
            if self.on_wait:
                self.on_wait(False)
            resp = self._request(path)
        if _blocked(resp):
            raise LetterboxdBlocked(
                f"Letterboxd answered {path} with a Cloudflare bot check (HTTP {resp.status_code})"
            )
        if resp.status_code == 404:
            raise LookupError(f"{BASE_URL}{path} does not exist (is the username/list public and spelled right?)")
        resp.raise_for_status()
        return resp.text

    def iter_films(self, path: str, max_pages: int) -> Iterator[Film]:
        self.truncated = False
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            soup = BeautifulSoup(self._get(path if page == 1 else f"{path}page/{page}/"), "html.parser")
            for el in soup.select("li.griditem [data-item-slug], li.posteritem [data-item-slug]"):
                slug = el["data-item-slug"]
                if slug in seen:
                    continue
                seen.add(slug)
                name = el.get("data-item-full-display-name") or el.get("data-item-name") or slug
                match = NAME_YEAR.match(name)
                yield Film(
                    slug=slug,
                    name=name,
                    title=match.group(1) if match else name,
                    year=int(match.group(2)) if match else None,
                )
            if not soup.select_one("a.next"):
                return
        self.truncated = True

    def watchlist_info(self, username: str) -> dict:
        soup = BeautifulSoup(self._get(f"/{username}/watchlist/"), "html.parser")
        count = None
        el = soup.select_one("[data-num-entries]")
        if el and el["data-num-entries"].isdigit():
            count = int(el["data-num-entries"])
        else:
            label = soup.select_one(".js-watchlist-count")
            digits = re.sub(r"\D", "", label.get_text()) if label else ""
            count = int(digits) if digits else None
        meta = soup.select_one('meta[name="description"]')
        match = re.match(r"^(.*?)[’']s Watchlist", meta.get("content", "")) if meta else None
        return {"count": count, "display_name": match.group(1).strip() if match else username}

    def tmdb(self, slug: str) -> tuple[int | None, str | None]:
        soup = BeautifulSoup(self._get(f"/film/{slug}/"), "html.parser")
        body = soup.body
        tmdb_id = body.get("data-tmdb-id") if body else None
        tmdb_type = body.get("data-tmdb-type") if body else None
        if not tmdb_id:
            link = soup.select_one('a[href*="themoviedb.org/"]')
            match = re.search(r"themoviedb\.org/(movie|tv)/(\d+)", link["href"]) if link else None
            if match:
                tmdb_type, tmdb_id = match.groups()
        return (int(tmdb_id) if tmdb_id else None, tmdb_type)
