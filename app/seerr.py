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
            {"id": u["id"], "name": u.get("displayName") or u.get("username") or u.get("email") or f"User {u['id']}"}
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
        body: dict = {"mediaType": "movie", "mediaId": tmdb_id, "is4k": is4k}
        if user_id:
            body["userId"] = user_id
        resp = self.client.post("/request", json=body)
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message") or resp.text
            except ValueError:
                msg = resp.text
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
