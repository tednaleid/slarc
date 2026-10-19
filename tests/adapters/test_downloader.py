# ABOUTME: Tests for HttpDownloader over httpx's MockTransport: auth, limits and parallelism.
# ABOUTME: Responses are served in memory; blobs go to the in-memory or filesystem blob store.
import hashlib
import threading
from pathlib import Path

import httpx

from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.downloader import HttpDownloader
from slarc.ports import BlobStore, DownloadJob, Session
from tests.fakes import InMemoryBlobStore

TEAM = "T0CB5LYPVFA"
SESSION = Session(TEAM, "naleidworkspace", "naleid", "desktop", "xoxc-test", "xoxd-test")
FILE_URL = "https://files.slack.com/files-pri/T0CB5LYPVFA-F0CA7TKM285/download/10k-snippet.txt"
GIPHY_URL = "https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif"
AVATAR_URL = "https://avatars.slack-edge.com/2026-10-19/avatar_512.png"
GITHUB_URL = "https://avatars.githubusercontent.com/u/5231?v=4?s=400"


def downloader(handler, blobs: BlobStore | None = None) -> HttpDownloader:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpDownloader(blobs or InMemoryBlobStore(), {TEAM: SESSION}, client)


def job(url: str, *, slack_auth: bool = False, max_bytes: int = 1000) -> DownloadJob:
    kind = "file" if slack_auth else "image"
    return DownloadJob(TEAM, kind, url, url, max_bytes, slack_auth)


def test_downloader_sends_auth_only_for_slack_hosts():
    seen: dict[str, tuple[str | None, str | None]] = {}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen[str(request.url)] = (
                request.headers.get("authorization"),
                request.headers.get("cookie"),
            )
        return httpx.Response(200, content=b"bytes")

    jobs = [
        job(FILE_URL, slack_auth=True),
        job(GIPHY_URL, slack_auth=True),
        job(AVATAR_URL),
        job(GITHUB_URL),
        job(FILE_URL.replace("10k", "other")),
    ]
    results = list(downloader(handler).fetch(jobs, 2))

    assert {r.status for r in results} == {"ok"}
    assert seen[FILE_URL] == ("Bearer xoxc-test", "d=xoxd-test")
    others = {url: headers for url, headers in seen.items() if url != FILE_URL}
    assert len(others) == 4
    assert set(others.values()) == {(None, None)}


def test_redirect_away_from_slack_drops_auth():
    seen: dict[str, tuple[str | None, str | None]] = {}
    moved = "https://files.slack.com/files-pri/moved/10k-snippet.txt"
    cdn = "https://cdn.example.com/10k-snippet.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen[url] = (request.headers.get("authorization"), request.headers.get("cookie"))
        if url == FILE_URL:
            return httpx.Response(302, headers={"location": moved})
        if url == moved:
            return httpx.Response(302, headers={"location": cdn})
        return httpx.Response(200, content=b"snippet")

    [result] = downloader(handler).fetch([job(FILE_URL, slack_auth=True)], 1)

    assert (result.status, result.size) == ("ok", 7)
    assert seen[moved] == ("Bearer xoxc-test", "d=xoxd-test")
    assert seen[cdn] == (None, None)


def test_downloader_streams_snippet_byte_identical(tmp_path: Path):
    data = (bytes(range(256)) * 900)[:228_893]
    chunks = [data[i : i + 65536] for i in range(0, len(data), 65536)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/force-download"}, content=iter(chunks)
        )

    blobs = FsBlobStore(tmp_path)
    [result] = downloader(handler, blobs).fetch(
        [job(FILE_URL, slack_auth=True, max_bytes=1_000_000)], 1
    )

    assert result.status == "ok"
    assert (result.sha256, result.size) == (hashlib.sha256(data).hexdigest(), 228_893)
    assert result.content_type == "application/force-download"
    assert blobs.read(hashlib.sha256(data).hexdigest()) == data


def test_downloader_aborts_over_max_bytes(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/declared":
            return httpx.Response(200, headers={"content-length": "5000"}, content=b"x" * 5000)
        return httpx.Response(200, content=iter([b"a" * 600, b"b" * 600, b"c" * 600]))

    blobs = FsBlobStore(tmp_path)
    jobs = [job("https://cdn.example.com/declared"), job("https://cdn.example.com/streamed")]
    results = list(downloader(handler, blobs).fetch(jobs, 2))

    assert {r.status for r in results} == {"too_large"}
    assert {r.sha256 for r in results} == {None}
    assert all(r.error and "1000" in r.error for r in results)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_downloader_maps_403_and_404_to_forbidden_and_other_errors_to_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(int(request.url.path.strip("/")))

    jobs = [job(f"https://cdn.example.com/{path}") for path in ("403", "404", "500", "down")]
    results = {r.job.url.rsplit("/", 1)[1]: r for r in downloader(handler).fetch(jobs, 2)}

    statuses = {path: r.status for path, r in results.items()}
    assert statuses == {"403": "forbidden", "404": "forbidden", "500": "failed", "down": "failed"}
    assert results["500"].error == "HTTP 500"
    assert results["down"].error
    assert all("xox" not in (r.error or "") for r in results.values())


def test_downloader_parallelism_bounded():
    lock = threading.Lock()
    active = 0
    peak = 0
    barrier = threading.Barrier(3, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return httpx.Response(200, content=request.url.path.encode())

    jobs = [job(f"https://cdn.example.com/{n}") for n in range(9)]
    results = list(downloader(handler).fetch(jobs, 3))

    assert len(results) == 9
    assert {r.status for r in results} == {"ok"}
    assert peak == 3
