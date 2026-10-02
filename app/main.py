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


class State:
    def __init__(self, client):
        self.client = client
        self.library = None
        self.library_at = 0.0
        self.library_lock = asyncio.Lock()
        self.health_ok = False
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
                    s.health_ok, s.health_at = False, time.monotonic()
                    log.warning("Plex request failed: %s", type(exc).__name__)
                    return JSONResponse({"error": "Plex did not answer"}, status_code=502)
                s.library_at = time.monotonic()
                s.health_ok, s.health_at = True, s.library_at
        return s.library

    @app.get("/health")
    async def health(response: Response):
        s = app.state.s
        if time.monotonic() - s.health_at >= HEALTH_TTL:
            try:
                await s.client.sections()
                s.health_ok = True
            except httpx.HTTPError as exc:
                log.warning("Plex health check failed: %s", type(exc).__name__)
                s.health_ok = False
            s.health_at = time.monotonic()
        if not s.health_ok:
            response.status_code = 503
        return {"status": "ok" if s.health_ok else "plex unreachable"}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
