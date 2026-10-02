"""Plex API client and the mapping from Plex metadata to the dashboard's item schema.

The mapping functions are pure (dict in, dict out) so they can be tested without Plex.
"""
import math

import httpx


def _x10(value):
    """A 0-10 Plex rating as a 0-100 integer, rounding half up like JS Math.round."""
    if value is None:
        return None
    return int(math.floor(float(value) * 10 + 0.5))


def _minutes(ms):
    if ms is None:
        return None
    return int(math.floor(ms / 60000 + 0.5))


def _versions(item):
    """One entry per Media object: a Media is a distinct version/encode of the title.

    A Media's Part list is the physical files of that one version. It is usually a
    single file, but a long film can be split across two (The Ten Commandments), and
    that is still one version. Never flatten Parts across Media and count them, or
    split files read as duplicate copies.
    """
    versions = []
    for media in item.get("Media") or []:
        files = [{"p": part.get("file", ""), "sz": int(part.get("size") or 0)}
                 for part in media.get("Part") or []]
        versions.append({
            "res": media.get("videoResolution"),
            "cd": media.get("videoCodec"),
            "sz": sum(f["sz"] for f in files),
            "files": files,
        })
    return versions


def _common(item):
    versions = _versions(item)
    return {
        "a": _x10(item.get("audienceRating")),
        "cr": item.get("contentRating") or "",
        "dur": _minutes(item.get("duration")),
        "added": item.get("addedAt"),
        "vc": item.get("viewCount") or 0,
        "sz": sum(v["sz"] for v in versions),
        "v": versions,
    }


def map_movie(item):
    return {
        "t": str(item.get("title", "")),
        "y": item.get("year"),
        "lib": "Movies",
        "sh": None,
        "se": None,
        "ep": None,
        "r": _x10(item.get("rating")),
        "g": [g.get("tag") for g in item.get("Genre") or []][:4],
        **_common(item),
    }


def map_episode(item):
    return {
        "t": str(item.get("title", "")),
        "y": item.get("year"),
        "lib": "TV",
        "sh": item.get("grandparentTitle"),
        "se": item.get("parentIndex"),
        "ep": item.get("index"),
        "r": None,  # critic score is not available at episode level
        "g": [],    # nor is genre
        **_common(item),
    }


class PlexClient:
    def __init__(self, base_url, token, timeout=60.0, transport=None):
        # The token travels only as a header, never in a URL, so it cannot end up in
        # an access log or an exception message that quotes the request.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json", "X-Plex-Token": token},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self):
        await self._client.aclose()

    async def _get(self, path, params=None):
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json().get("MediaContainer") or {}

    async def sections(self):
        return (await self._get("/library/sections")).get("Directory") or []

    async def library(self):
        """Every movie and every episode, mapped to the dashboard schema."""
        items = []
        for section in await self.sections():
            key, kind = section.get("key"), section.get("type")
            if kind == "movie":
                data = await self._get(f"/library/sections/{key}/all")
                items.extend(map_movie(m) for m in data.get("Metadata") or [])
            elif kind == "show":
                # type=4 returns episodes directly rather than shows.
                data = await self._get(f"/library/sections/{key}/all", params={"type": 4})
                items.extend(map_episode(e) for e in data.get("Metadata") or [])
        return items
