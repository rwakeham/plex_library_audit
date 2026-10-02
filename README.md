# Plex Library Audit

A small always-on dashboard showing what is using space in the Plex library: totals,
sortable Movies and TV tables, an ncdu-style folder tree, and duplicate versions. Data
comes live from Plex's API (cached for 5 minutes). Sizes are binary GiB/TiB, matching
ncdu and Synology DSM.

LAN-only, no authentication. The Plex token stays on the server and never reaches the
browser.

## Quick start

On Rasputin:

```bash
git clone git@github.com:rwakeham/plex_library_audit.git ~/plex_library_audit
cd ~/plex_library_audit
./setup.sh
```

`setup.sh` asks for your Plex token and server URL, finds the first free port from 8300
(skipping ports other containers publish and anything already listening), writes `.env`,
then hands off to `./deploy.sh --skip-git`. Re-running it keeps existing values and offers
to replace the token.

## Deploying

```bash
./deploy.sh                      # merge the pending claude/* branch into main, build, start, check
./deploy.sh --branch claude/x    # choose the branch when more than one is pending (required without a terminal)
./deploy.sh --skip-git           # redeploy the current checkout untouched
```

With exactly one `claude/*` branch ahead of `main`, `deploy.sh` merges it without asking.
With several, it prompts, or fails asking for `--branch` when there is no terminal. With
none, it deploys `origin/main`. The deploy succeeds only once `/health` reports that the
app can reach Plex.

To apply a `.env` change, run `./deploy.sh --skip-git`. Restarting the container does
not re-read `.env`, and starting it by hand skips the health check.

## Endpoints

| Path | What |
|---|---|
| `/` | The dashboard |
| `/api/library` | Every movie and episode as JSON; `?refresh=1` bypasses the cache |
| `/health` | 200 when Plex answers with the configured token, 503 otherwise |

## Configuration

`.env` (written by `setup.sh`; see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `PLEX_TOKEN` | required | Plex auth token |
| `PLEX_BASE_URL` | `http://172.16.19.6:32400` | Plex server |
| `PORT` | `8300` | Host port for the dashboard |
| `CACHE_TTL` | `300` | Seconds to cache the library |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```
