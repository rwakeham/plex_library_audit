# Plex Library Audit

A small self-hosted dashboard that shows what is using space in a Plex library: totals,
sortable Movies and TV tables, an ncdu-style folder tree, and movies stored in more than
one version. Data comes live from Plex's API and is cached for 5 minutes. Sizes are
binary units (GiB/TiB), the same as `ncdu` and most NAS interfaces show.

It is meant for a home network: there is no authentication. The Plex token stays on the
server and never reaches the browser.

## Requirements

- Docker with Compose v2 (`docker compose`)
- A Plex Media Server reachable from the Docker host
- A Plex token. In Plex Web, open any item, choose **Get Info → View XML**, and copy the
  `X-Plex-Token` value from the URL.

## Quick start

```bash
git clone <this-repository-url> plex_library_audit
cd plex_library_audit
./setup.sh
```

`setup.sh` asks for your Plex token and server URL (for example
`http://192.168.1.10:32400`). It then finds the first free port from 8300, skipping ports
other containers publish and anything already listening, and writes `.env` (readable
only by you). Finally it hands off to `./deploy.sh --skip-git`. Re-running it keeps
existing values and offers to replace the token.

When it finishes, open `http://<docker-host>:<port>`.

## Deploying

```bash
./deploy.sh --skip-git           # build and redeploy the current checkout
./deploy.sh                      # update from origin first (see below), then build and deploy
./deploy.sh --branch claude/x    # as above, choosing which pending branch to merge
```

The deploy builds the image, starts the container, and waits for the app to answer
`/health`. It then checks Plex once and only warns if Plex is down. The deploy still
succeeds, and the dashboard shows the Plex error until Plex is reachable.

Without `--skip-git`, `deploy.sh` resets the checkout's `main` to `origin/main`. It
refuses if that would lose local commits or uncommitted edits. If any `claude/*`
branches are ahead of `main`, it merges one and pushes the result. With several, it
prompts, or asks for `--branch` when run without a terminal.

To apply a `.env` change, run `./deploy.sh --skip-git`. Restarting the container does
not re-read `.env`, and starting it by hand skips the health check.

## Endpoints

| Path | What |
|---|---|
| `/` | The dashboard |
| `/api/library` | Every movie and episode as JSON; `?refresh=1` bypasses the cache; 502 with an `error` message if Plex fails |
| `/health` | Liveness: 200 whenever the app is up, whatever Plex's state |
| `/health/deps` | 200 `{"plex":"ok"}` when Plex answers with the token; 503 with `unreachable` or `unauthorized` otherwise. Point an uptime monitor here |

## Configuration

`.env` (written by `setup.sh`; see `.env.example`):

| Variable | Meaning |
|---|---|
| `PLEX_TOKEN` | Plex auth token (required) |
| `PLEX_BASE_URL` | URL of your Plex server, such as `http://192.168.1.10:32400` |
| `PORT` | Host port for the dashboard (`setup.sh` suggests the first free one from 8300) |
| `CACHE_TTL` | Seconds to cache the library (default 300) |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```
