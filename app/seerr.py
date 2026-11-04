from urllib.parse import quote

import httpx

IN_SEERR = {
    2: "Requested, waiting for approval",
    3: "Requested and processing",
    4: "Partially available",
    5: "Available",
}
SKIPPED = {
    6: "Blocklisted in Seerr",
    7: "Deleted in Seerr",
}
REQUEST_DECLINED = 3
REQUEST_PENDING = 1

ADMIN, MANAGE_REQUESTS, REQUEST, AUTO_APPROVE, AUTO_APPROVE_MOVIE = 2, 16, 32, 128, 256
REQUEST_4K, REQUEST_4K_MOVIE, AUTO_APPROVE_4K, AUTO_APPROVE_4K_MOVIE, REQUEST_MOVIE = 1024, 2048, 32768, 65536, 262144


class QuotaReached(Exception):
    pass


def approval(permissions: int | None, is4k: bool) -> str | None:
    if permissions is None:
        return None
    if permissions & ADMIN:
        return "auto"
    can_request = (REQUEST_4K | REQUEST_4K_MOVIE) if is4k else (REQUEST | REQUEST_MOVIE)
    auto = (AUTO_APPROVE_4K | AUTO_APPROVE_4K_MOVIE) if is4k else (AUTO_APPROVE | AUTO_APPROVE_MOVIE)
    if not permissions & can_request:
        return "none"
    return "auto" if permissions & (auto | MANAGE_REQUESTS) else "manual"


class Seerr:
    def __init__(self, url: str, api_key: str) -> None:
        self.client = httpx.Client(
            base_url=f"{url}/api/v1",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=30,
        )

    def me(self) -> dict:
        resp = self.client.get("/auth/me")
        if resp.status_code in (401, 403):
            raise PermissionError("Seerr rejected the API key")
        resp.raise_for_status()
        return resp.json()

    def version(self) -> str | None:
        resp = self.client.get("/status")
        return resp.json().get("version") if resp.is_success else None

    def users(self) -> list[dict]:
        resp = self.client.get("/user?take=500&skip=0&sort=displayname")
        resp.raise_for_status()
        return [
            {"id": u["id"], "name": u.get("displayName") or u.get("username") or u.get("email") or f"User {u['id']}",
             "permissions": u.get("permissions")}
            for u in resp.json().get("results", [])
        ]

    def movie(self, tmdb_id: int) -> dict | None:
        resp = self.client.get(f"/movie/{tmdb_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def search_movie(self, title: str, year: int | None) -> dict | None:
        resp = self.client.get(f"/search?query={quote(title, safe='')}&page=1")
        resp.raise_for_status()
        movies = [r for r in resp.json().get("results", []) if r.get("mediaType") == "movie"]
        if year:
            movies = [m for m in movies if (m.get("releaseDate") or "")[:4] in (str(year), str(year - 1), str(year + 1))]
        exact = [m for m in movies if (m.get("title") or "").casefold() == title.casefold()]
        return (exact or movies or [None])[0]

    def request_movie(self, tmdb_id: int, is4k: bool, user_id: int | None) -> dict:
        headers = {"X-API-User": str(user_id)} if user_id else {}
        body = {"mediaType": "movie", "mediaId": tmdb_id, "is4k": is4k}
        resp = self.client.post("/request", json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message") or resp.text
            except ValueError:
                msg = resp.text
            if resp.status_code == 403 and "quota" in msg.lower():
                raise QuotaReached(msg)
            raise RuntimeError(f"Seerr refused the request (HTTP {resp.status_code}): {msg}")
        return resp.json()


def existing_state(movie: dict, is4k: bool) -> tuple[str, str] | None:
    info = movie.get("mediaInfo")
    if not info:
        return None
    status = info.get("status4k" if is4k else "status") or 1
    if status in IN_SEERR:
        return "in_seerr", IN_SEERR[status]
    if status in SKIPPED:
        return "skipped", SKIPPED[status]
    requests = [r for r in info.get("requests") or [] if bool(r.get("is4k")) == is4k]
    if any(r.get("status") == REQUEST_DECLINED for r in requests):
        return "skipped", "Request was declined"
    if requests:
        return "in_seerr", "Already requested"
    return None
