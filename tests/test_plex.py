"""Tests for the Plex mapping and the API, against a fake Plex (no network)."""
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402
from app.plex import PlexClient, map_episode, map_movie  # noqa: E402

INTERSTELLAR = {
    "title": "Interstellar", "year": 2014, "rating": 7.2, "audienceRating": 8.6,
    "contentRating": "PG-13", "duration": 10140000, "addedAt": 1716593622,
    "Genre": [{"tag": "Adventure"}, {"tag": "Drama"}, {"tag": "Science Fiction"}],
    "Media": [{"videoResolution": "4k", "videoCodec": "hevc", "Part": [
        {"file": "/media/Movies/Interstellar.mkv", "size": 21229500651}]}],
}
# One version split across two files: must count as one version, not a duplicate.
TEN_COMMANDMENTS = {
    "title": "The Ten Commandments", "year": 1956, "viewCount": 2,
    "Genre": [{"tag": t} for t in ["Drama", "History", "Adventure", "Epic", "Extra"]],
    "Media": [{"videoResolution": "1080", "videoCodec": "h264", "Part": [
        {"file": "/m/TenC.cd1.mkv", "size": 100}, {"file": "/m/TenC.cd2.mkv", "size": 50}]}],
}
# Two versions: a real duplicate.
NINETEEN_SEVENTEEN = {
    "title": "1917", "year": 2019,
    "Media": [
        {"videoResolution": "1080", "videoCodec": "h264", "Part": [{"file": "/m/a.mp4", "size": 7}]},
        {"videoResolution": "1080", "videoCodec": "h264", "Part": [{"file": "/m/b.mkv", "size": 5}]},
    ],
}
EPISODE = {
    "title": "Pilot", "grandparentTitle": "Some Show", "parentIndex": 1, "index": 1,
    "audienceRating": 7.95, "rating": 9.0, "duration": 2700000, "addedAt": 1,
    "Genre": [{"tag": "Drama"}],
    "Media": [{"videoResolution": "720", "videoCodec": "h264", "Part": [{"file": "/tv/s1e1.mkv", "size": 3}]}],
}


def test_movie_matches_documented_schema():
    assert map_movie(INTERSTELLAR) == {
        "t": "Interstellar", "y": 2014, "lib": "Movies", "sh": None, "se": None, "ep": None,
        "r": 72, "a": 86, "cr": "PG-13", "g": ["Adventure", "Drama", "Science Fiction"],
        "dur": 169, "added": 1716593622, "vc": 0, "sz": 21229500651,
        "v": [{"res": "4k", "cd": "hevc", "sz": 21229500651, "files": [
            {"p": "/media/Movies/Interstellar.mkv", "sz": 21229500651}]}],
    }


def test_split_file_movie_is_one_version():
    m = map_movie(TEN_COMMANDMENTS)
    assert len(m["v"]) == 1
    assert len(m["v"][0]["files"]) == 2
    assert m["v"][0]["sz"] == m["sz"] == 150
    assert m["g"] == ["Drama", "History", "Adventure", "Epic"]
    assert m["r"] is None and m["a"] is None and m["cr"] == "" and m["vc"] == 2


def test_two_media_is_two_versions():
    m = map_movie(NINETEEN_SEVENTEEN)
    assert [v["sz"] for v in m["v"]] == [7, 5]
    assert m["sz"] == 12


def test_episode_mapping():
    e = map_episode(EPISODE)
    assert (e["lib"], e["sh"], e["se"], e["ep"]) == ("TV", "Some Show", 1, 1)
    assert e["r"] is None and e["g"] == []
    assert e["a"] == 80  # 79.5 rounds half up, like the original JS
    assert e["dur"] == 45 and e["sz"] == 3


class FakePlex:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.reject = False

    def __call__(self, request):
        self.calls.append(request)
        if self.fail:
            raise httpx.ConnectError("down")
        if self.reject:
            return httpx.Response(401)
        assert request.headers["X-Plex-Token"] == "tok"
        assert request.headers["Accept"] == "application/json"
        path = request.url.path
        if path == "/library/sections":
            body = {"Directory": [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"},
                                  {"key": "3", "type": "artist"}]}
        elif path == "/library/sections/1/all":
            body = {"Metadata": [INTERSTELLAR, TEN_COMMANDMENTS]}
        elif path == "/library/sections/2/all":
            assert request.url.params.get("type") == "4"
            body = {"Metadata": [EPISODE]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"MediaContainer": body})


@pytest.fixture
def plex():
    return FakePlex()


@pytest.fixture
def client(plex):
    app = main.create_app(PlexClient("http://plex", "tok", transport=httpx.MockTransport(plex)))
    with TestClient(app) as c:
        yield c


def test_library_endpoint(client):
    r = client.get("/api/library")
    assert r.status_code == 200
    data = r.json()
    assert [d["t"] for d in data] == ["Interstellar", "The Ten Commandments", "Pilot"]
    assert [d["lib"] for d in data] == ["Movies", "Movies", "TV"]


def test_library_is_cached_and_refreshable(client, plex):
    client.get("/api/library")
    n = len(plex.calls)
    client.get("/api/library")
    assert len(plex.calls) == n
    client.get("/api/library?refresh=1")
    assert len(plex.calls) == 2 * n


def test_library_502_when_plex_down(client, plex):
    plex.fail = True
    r = client.get("/api/library")
    assert r.status_code == 502
    assert "Can't reach Plex" in r.json()["error"]
    assert "tok" not in r.text


def test_library_names_a_rejected_token(client, plex):
    plex.reject = True
    r = client.get("/api/library")
    assert r.status_code == 502
    assert "rejected the token" in r.json()["error"]


@pytest.fixture
def no_health_cache():
    main.HEALTH_TTL, saved = 0, main.HEALTH_TTL
    yield
    main.HEALTH_TTL = saved


def test_health_is_liveness_only(client, plex):
    # A deploy must succeed while Plex is down: /health does not call Plex at all.
    plex.fail = True
    n = len(plex.calls)
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert len(plex.calls) == n


def test_health_deps_reports_plex(client, plex, no_health_cache):
    r = client.get("/health/deps")
    assert r.status_code == 200 and r.json() == {"plex": "ok"}
    plex.fail = True
    r = client.get("/health/deps")
    assert r.status_code == 503 and r.json() == {"plex": "unreachable"}
    plex.fail, plex.reject = False, True
    assert client.get("/health/deps").json() == {"plex": "unauthorized"}


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "fetch('/api/library'" in r.text
    assert "const DATA = [" not in r.text


def test_icons_served(client):
    r = client.get("/icon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    r = client.get("/apple-touch-icon.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
