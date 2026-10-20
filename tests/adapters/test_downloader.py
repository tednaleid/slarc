# ABOUTME: Tests for HttpDownloader over httpx's MockTransport: auth, limits and parallelism.
# ABOUTME: Responses are served in memory; blobs go to the in-memory or filesystem blob store.
import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.downloader import OUTSIDE_TIMEOUT_S, HttpDownloader
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

    statuses = {(r.job.url, r.job.slack_auth): r.status for r in results}
    assert statuses.pop((GIPHY_URL, True)) == "failed"
    assert set(statuses.values()) == {"ok"}
    assert seen[FILE_URL] == ("Bearer xoxc-test", "d=xoxd-test")
    others = {url: headers for url, headers in seen.items() if url != FILE_URL}
    assert len(others) == 4
    assert set(others.values()) == {(None, None)}


def test_redirect_on_slack_keeps_auth_and_off_slack_drops_it(tmp_path: Path):
    seen: dict[str, tuple[str | None, str | None]] = {}
    moved = "https://files.slack.com/files-pri/moved/10k-snippet.txt"
    leaving = FILE_URL.replace("10k", "leaving")
    cdn = "https://cdn.example.com/10k-snippet.txt"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen[url] = (request.headers.get("authorization"), request.headers.get("cookie"))
        if url == FILE_URL:
            return httpx.Response(302, headers={"location": moved})
        if url == leaving:
            return httpx.Response(302, headers={"location": cdn})
        return httpx.Response(200, content=b"snippet")

    blobs = FsBlobStore(tmp_path)
    jobs = [job(FILE_URL, slack_auth=True), job(leaving, slack_auth=True)]
    results = {r.job.url: r for r in downloader(handler, blobs).fetch(jobs, 1)}

    assert (results[FILE_URL].status, results[FILE_URL].size) == ("ok", 7)
    assert seen[moved] == ("Bearer xoxc-test", "d=xoxd-test")
    assert seen[cdn] == (None, None)
    left = results[leaving]
    assert (left.status, left.sha256) == ("failed", None)
    assert left.error and "session may have expired" in left.error
    assert "cdn.example.com" not in left.error
    stored = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(stored) == 1


def test_slack_sign_in_page_is_not_stored(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        page = b"<html><body>Sign in to Slack</body></html>"
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=page
        )

    blobs = FsBlobStore(tmp_path)
    [result] = downloader(handler, blobs).fetch([job(FILE_URL, slack_auth=True)], 1)

    assert (result.status, result.sha256) == ("failed", None)
    assert result.error and "session may have expired" in result.error
    assert "xox" not in result.error and "files.slack.com" not in result.error
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_html_image_off_slack_is_stored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html/>")

    [result] = downloader(handler).fetch([job(GITHUB_URL)], 1)

    assert result.status == "ok"


def test_downloader_fails_malformed_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"never")

    bad = ["https://[::1/direct", "https://exa mple.com/\x00"]
    results = list(downloader(handler).fetch([job(url) for url in bad], 2))

    assert [r.status for r in results] == ["failed", "failed"]
    assert all(r.error and "[::1" not in r.error for r in results)


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


def test_closing_fetch_stops_an_in_flight_download(tmp_path: Path):
    streaming = threading.Event()

    def slow_body() -> Iterator[bytes]:
        for _ in range(2000):
            streaming.set()
            yield b"x" * 1024
            time.sleep(0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            return httpx.Response(200, content=slow_body())
        return httpx.Response(200, content=b"fast")

    jobs = [
        job("https://cdn.example.com/slow", max_bytes=10**7),
        job("https://cdn.example.com/fast"),
    ]
    results = downloader(handler, FsBlobStore(tmp_path)).fetch(jobs, 2)

    assert next(results).job.url.endswith("/fast")
    assert streaming.wait(5)
    started = time.monotonic()
    results.close()

    assert time.monotonic() - started < 1.0
    assert list(tmp_path.rglob(".tmp-*")) == []


def test_outside_sites_get_a_shorter_timeout_than_slack_files():
    timeouts: dict[str, dict] = {}
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            timeouts[str(request.url)] = request.extensions["timeout"]
        return httpx.Response(200, content=b"bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)
    fetcher = HttpDownloader(InMemoryBlobStore(), {TEAM: SESSION}, client)

    list(fetcher.fetch([job(FILE_URL, slack_auth=True), job(GITHUB_URL)], 2))

    assert set(timeouts[FILE_URL].values()) == {60.0}
    assert set(timeouts[GITHUB_URL].values()) == {OUTSIDE_TIMEOUT_S}
    assert OUTSIDE_TIMEOUT_S == 15.0
