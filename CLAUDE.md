# CLAUDE.md

Developer guide for AI-assisted work on Plex Library Audit.

## Project overview

A LAN-only dashboard showing what is taking up space in the Plex library on Rasputin:
overview stats, sortable Movies and TV tables, an ncdu-style folder tree, and a duplicates
panel. Stateless: every number comes live from Plex's API. Python 3.12, FastAPI, httpx, a
single static HTML page, Docker Compose.

## Repository layout

```
app/                    — the FastAPI application
  __init__.py
  main.py               — routes (/, /api/library, /health), cache, settings from env
  plex.py               — Plex client and the Plex → dashboard item mapping
  static/
    index.html          — the dashboard (HTML/CSS/JS in one file), fetches /api/library
tests/
  test_plex.py          — mapping and API tests against a fake Plex
  test_deploy_script.py — deploy.sh behaviour harness, from the rw-coding-compliance skill
deploy.sh               — deployment pipeline (follows the rw-coding-compliance standard)
setup.sh                — config wizard; writes .env (token, URL, free port), hands off to deploy.sh
docker-compose.yml      — the single app service
Dockerfile              — app image
requirements.txt        — runtime Python dependencies
requirements-dev.txt    — adds pytest
.env.example            — every variable, with placeholders
```

## Running locally

With Docker, on a host that can reach Plex:

```bash
./setup.sh                  # first run: prompts for the token, picks a free port, deploys
./deploy.sh --skip-git      # redeploy the current checkout
```

Without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PLEX_TOKEN=... PLEX_BASE_URL=http://172.16.19.6:32400 .venv/bin/uvicorn app.main:app --port 8300
.venv/bin/python -m pytest tests/
```

## Architecture

1. The browser loads `/`, which serves `app/static/index.html`.
2. The page calls `GET /api/library` (`?refresh=1` when the "refresh now" link is used).
3. `app/main.py` returns the cached array if it is younger than `CACHE_TTL`; otherwise it
   asks `PlexClient.library()` in `app/plex.py`, which calls `GET /library/sections`, then
   `/library/sections/{key}/all` for each movie section and `…/all?type=4` (episodes,
   not shows) for each show section, and maps every item with `map_movie`/`map_episode`.
4. The page wraps the original artifact's render code in `startDashboard(DATA)` and runs
   it once the array arrives. All aggregation (totals, duplicates, the tree) happens in
   the browser.

`/health` calls `/library/sections` (cached for 15 s) and answers 200 only if Plex
answered with the configured token, otherwise 503. A successful `/api/library` fetch also
marks Plex healthy.

## Key conventions

- **A version is a `Media`, not a `Part`.** `v` has one entry per `Media`; each Media's
  `Part`s are its files. Some long films are split across two files that are one version
  (The Ten Commandments). Flattening Parts into one list flags them as duplicates.
  `tests/test_plex.py` pins this.
- **The API schema matches the original artifact's inline `DATA` exactly.** The
  frontend's render code is the artifact's, unchanged; a field rename breaks it silently.
  Change the schema and the page together.
- **Ratings round half up** (`floor(x*10 + 0.5)`), matching JS `Math.round` in the
  original export. Python's `round()` rounds half to even and would shift some scores by 1.
- **The Plex token only travels as a header** and is never logged. Logs carry only the
  exception type, never the request, so a token can't leak through a URL or an error message.
- **One uvicorn worker.** The cache is in process memory; a second worker would hold its
  own copy and double the Plex calls.
- **Sizes are binary (GiB/TiB).** `fmtSize` divides by 1024 to match ncdu and Synology DSM,
  which is what the owner reads. Do not "fix" it to decimal.

## Common tasks

### Add a field to the dashboard

1. Map it in `map_movie`/`map_episode` (or `_common`) in `app/plex.py`.
2. Add it to the expected dict in `test_movie_matches_documented_schema`.
3. Render it inside `startDashboard` in `app/static/index.html`.
4. `pytest tests/`, then `./deploy.sh`.

### Change the cache duration

Set `CACHE_TTL` in `.env`, then `./deploy.sh --skip-git` (a restart does not re-read `.env`).

### Rotate the Plex token

Run `./setup.sh` and answer yes to "Replace it?". It redeploys afterwards.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PLEX_TOKEN` | *(required)* | Plex auth token, sent as `X-Plex-Token`. The app refuses to start without it |
| `PLEX_BASE_URL` | `http://172.16.19.6:32400` | Plex server to read from |
| `PORT` | `8300` | Host port the dashboard is published on; `setup.sh` picks the first free one from 8300 |
| `CACHE_TTL` | `300` | Seconds to keep the library in memory between Plex fetches |

`.env.example` lists these with placeholder values; compose passes each one to the
container (`PORT` is used by compose itself for the port mapping).

## Deployment

Follows the deployment standard carried by the `rw-coding-compliance` skill. Project
specifics:

- **Lives at** `/home/ryan/plex_library_audit` on Rasputin (`172.16.19.6`), port 8300 by
  default. No database and no volumes, so the existing restic backup of `/home/ryan`
  (`.env` included) covers it with no change to the backup script.
- **Migrations** none: there is no database.
- **Host grants** none: no socket, host-path or namespace mounts.
- **Services** one, `app`. `deploy.sh` waits up to 60 s for `curl -sf /health`, which
  passes only once the app has reached Plex with the configured token, so a deploy fails
  if Plex is down or the token is wrong. Compose also runs a healthcheck against
  `/health`, and Uptime Kuma should monitor the same URL.
- **Port selection** `setup.sh` lists ports other containers publish (`docker ps`) and
  listening sockets (`ss`), excludes this project's own container, and offers the first
  free port from 8300. An existing `PORT` in `.env` is kept.
- **Deployed by** hand only; not in the Pathway deploy agent's allowlist as of 2026-10-02.
