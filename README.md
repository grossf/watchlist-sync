<p align="center"><img src="app/static/icon.svg" width="96" alt=""></p>

<h1 align="center">Watchlist Sync</h1>

Once a day, this app reads your public Letterboxd watchlist and requests every film that Seerr doesn't already have. A film is left alone if it's available, requested, processing, deleted or blocklisted. Everything is set up in the web GUI. It also lets you sync by hand, do a dry run, and read a log that shows which films it could not find on Seerr.

## Setup

Copy [`docker-compose.yml`](docker-compose.yml) to your server, set `TZ` to your time zone, then start it:

```bash
docker compose up -d
```

The image is `ghcr.io/grossf/watchlist-sync` for amd64 and arm64. `latest` follows the newest release, or pin a version such as `v0.1.0`.

Open `http://<server>:8080`. On first launch the Watchlists page walks you through two steps:

1. **Connect Seerr** (Connections page): enter the URL and API key (Seerr → Settings → General → API Key), then click **Test connection**. Use an address the container can reach, such as `http://192.168.1.10:5055`, or `http://seerr:5055` if both containers share a Docker network. `localhost` means the container itself.
2. **Add your first watchlist** with the **+** button. The username is checked on Letterboxd before it's added, and the watchlist must be public. Pick the Seerr user it requests as: requests follow that user's Seerr permissions, so an admin's are auto-approved while a regular user's wait for your approval. You can change it later in edit mode.

Dry run is on for a fresh install: do a first sync, check the Logs page, then turn dry run off in **Settings**. Settings also holds the daily sync time and an optional password. Set one if the app is reachable from outside your home network, because the page can use your Seerr API key.

Each watchlist card shows a bar with the result of its last sync: in Seerr (green), requested (blue), skipped (grey: deleted, blocklisted, declined or TV), not found on Seerr (amber) and failed (red). Click a number to open the matching log entries.

The only environment variable is `TZ` (set in `docker-compose.yml`), which controls the sync time and log timestamps. Settings, log and cache live in `./data/app.db`.

## How it works

1. It reads your watchlist page by page, most recently added first. Letterboxd has no public API, so it reads the normal public pages.
2. For each film it reads the TMDB ID from the Letterboxd film page. These IDs are cached, so each film page is fetched only once.
3. It looks the film up in Seerr by TMDB ID. If Seerr doesn't know that ID, it searches Seerr by title and year.
4. It leaves the film alone if Seerr already has it (available, partially available, pending, processing or requested) or if it was deleted, blocklisted or declined there.

The first sync of a large watchlist takes a while because each film page is read once, about 2 s per film. Later syncs only read the watchlist pages plus any new films.

## Forgot the password

```bash
docker compose exec watchlist-sync python -c "from app import auth; auth.set_password(None)"
```

## Updating

```bash
docker compose pull && docker compose up -d
```

## Building it yourself

In a clone of this repo, replace `image:` with `build: .` in `docker-compose.yml`, then run:

```bash
docker compose up -d --build
```

## Running without Docker

```bash
pip install -r requirements.txt
```

```bash
DATA_DIR=./data uvicorn app.main:app --port 8080
```
