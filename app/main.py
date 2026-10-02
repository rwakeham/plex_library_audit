"""Plex Library Audit: serves the dashboard and a live JSON view of the Plex library."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse

from app.plex import PlexClient

log = logging.getLogger("plex_library_audit")

PLEX_BASE_URL = os.environ.get("PLEX_BASE_URL", "http://172.16.19.6:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))
# Health answers are cached briefly so the compose healthcheck and Uptime Kuma do not
# each hit Plex on every probe.
HEALTH_TTL = 15

STATIC_DIR = Path(__file__).parent / "static"

# Shown to the user on the page, so each names the fix. The token is never included.
PLEX_ERRORS = {
    "unauthorized": "Plex rejected the token. Run ./setup.sh on the server to replace it.",
    "unreachable": f"Can't reach Plex at {PLEX_BASE_URL}. Check that the Plex server is running.",
    "error": "Plex returned an error. Check the Plex server, then try again.",
}


def plex_status(exc):
    """Classify a failed Plex call as unauthorized, unreachable or error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return "unauthorized" if exc.response.status_code in (401, 403) else "error"
    return "unreachable"


class State:
    def __init__(self, client):
        self.client = client
        self.library = None
        self.library_at = 0.0
        self.library_lock = asyncio.Lock()
        self.plex_status = "unknown"
        self.health_at = 0.0


def create_app(client=None):
    @asynccontextmanager
    async def lifespan(app):
        if client is None and not PLEX_TOKEN:
            raise RuntimeError("PLEX_TOKEN is not set; run ./setup.sh")
        app.state.s = State(client or PlexClient(PLEX_BASE_URL, PLEX_TOKEN))
        yield
        await app.state.s.client.aclose()

    app = FastAPI(title="Plex Library Audit", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/api/library")
    async def library(refresh: bool = False):
        s = app.state.s
        # One fetch at a time: concurrent page loads on a cold cache share one Plex call.
        async with s.library_lock:
            fresh = s.library is not None and time.monotonic() - s.library_at < CACHE_TTL
            if refresh or not fresh:
                try:
                    s.library = await s.client.library()
                except httpx.HTTPError as exc:
                    s.plex_status, s.health_at = plex_status(exc), time.monotonic()
                    log.warning("Plex request failed: %s", type(exc).__name__)
                    return JSONResponse({"error": PLEX_ERRORS[s.plex_status]}, status_code=502)
                s.library_at = time.monotonic()
                s.plex_status, s.health_at = "ok", s.library_at
        return s.library

    @app.get("/health")
    async def health():
        # Liveness only: the app is up and serving. Deliberately independent of Plex,
        # so a deploy (and the compose healthcheck) succeeds while Plex is down; the
        # page then shows the Plex error itself. Plex reachability is /health/deps.
        return {"status": "ok"}

    @app.get("/health/deps")
    async def health_deps(response: Response):
        s = app.state.s
        if time.monotonic() - s.health_at >= HEALTH_TTL:
            try:
                await s.client.sections()
                s.plex_status = "ok"
            except httpx.HTTPError as exc:
                log.warning("Plex health check failed: %s", type(exc).__name__)
                s.plex_status = plex_status(exc)
            s.health_at = time.monotonic()
        if s.plex_status != "ok":
            response.status_code = 503
        return {"plex": s.plex_status}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
