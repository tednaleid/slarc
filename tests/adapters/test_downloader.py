# ABOUTME: Tests for HttpDownloader over httpx's MockTransport: auth, limits, parallelism and
# ABOUTME: per-host pacing. Responses are served in memory, blobs kept in a blob store.
import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.downloader import (
    MAX_RETRY_AFTER_S,
    OUTSIDE_HOST_PARALLELISM,
    OUTSIDE_TIMEOUT_S,
    RATE_LIMIT_RETRIES,
    HostHolds,
    HttpDownloader,
)
from slarc.ports import BlobStore, DownloadJob, Session
from tests.fakes import InMemoryBlobStore

TEAM = "T0CB5LYPVFA"
SESSION = Session(TEAM, "naleidworkspace", "naleid", "desktop", "xoxc-test", "xoxd-test")
FILE_URL = "https://files.slack.com/files-pri/T0CB5LYPVFA-F0CA7TKM285/download/10k-snippet.txt"
GIPHY_URL = "https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif"
AVATAR_URL = "https://avatars.slack-edge.com/2026-10-19/avatar_512.png"
GITHUB_URL = "https://avatars.githubusercontent.com/u/5231?v=4?s=400"


class FakeTime:
    """A clock that moves only when a download pauses, recording each pause."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def clock(self) -> float:
        return self.now

    def pause(self, seconds: float, cancel: threading.Event) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def downloader(
    handler, blobs: BlobStore | None = None, holds: HostHolds | None = None
) -> HttpDownloader:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpDownloader(blobs or InMemoryBlobStore(), {TEAM: SESSION}, client, holds)


def held(time_: FakeTime) -> HostHolds:
    return HostHolds(clock=time_.clock, pause=time_.pause)


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


def test_downloader_maps_403_to_forbidden_404_to_not_found_and_other_errors_to_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/down":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(int(request.url.path.strip("/")))

    paths = ("403", "404", "410", "500", "599", "down")
    jobs = [job(f"https://cdn.example.com/{path}") for path in paths]
    results = {r.job.url.rsplit("/", 1)[1]: r for r in downloader(handler).fetch(jobs, 2)}

    statuses = {path: r.status for path, r in results.items()}
    assert statuses == {
        "403": "forbidden",
        "404": "not_found",
        "410": "failed",
        "500": "failed",
        "599": "failed",
        "down": "failed",
    }
    errors = {path: r.error for path, r in results.items()}
    assert errors["403"] == "HTTP 403 Forbidden"
    assert errors["404"] == "HTTP 404 Not Found"
    assert errors["410"] == "HTTP 410 Gone"
    assert errors["500"] == "HTTP 500 Internal Server Error"
    assert errors["599"] == "HTTP 599"
    assert results["down"].error
    assert all("xox" not in (r.error or "") for r in results.values())


def test_slack_files_use_the_full_parallelism():
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

    jobs = [job(f"{FILE_URL}-{n}", slack_auth=True) for n in range(9)]
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


def test_each_outside_host_gets_at_most_two_downloads_at_a_time():
    lock = threading.Lock()
    active: dict[str, int] = {}
    peaks: dict[str, int] = {}
    overall = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal overall
        host = request.url.host
        with lock:
            active[host] = active.get(host, 0) + 1
            peaks[host] = max(peaks.get(host, 0), active[host])
            overall = max(overall, sum(active.values()))
        time.sleep(0.05)
        with lock:
            active[host] -= 1
        return httpx.Response(200, content=request.url.path.encode())

    hosts = ("opengraph.githubassets.com", "upload.wikimedia.org")
    jobs = [job(f"https://{host}/{n}") for host in hosts for n in range(6)]
    results = list(downloader(handler).fetch(jobs, 8))

    assert {r.status for r in results} == {"ok"}
    assert len(results) == 12
    assert OUTSIDE_HOST_PARALLELISM == 2
    assert peaks == {host: 2 for host in hosts}
    assert overall == 4


def test_rate_limited_download_waits_the_retry_after_then_retries():
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    time_ = FakeTime()

    results = list(
        downloader(lambda _: next(answers), holds=held(time_)).fetch([job(GITHUB_URL)], 1)
    )

    assert [r.status for r in results] == ["ok"]
    assert time_.pauses == [7]


def test_rate_limited_download_without_retry_after_waits_five_seconds():
    answers = iter([httpx.Response(429), httpx.Response(200)])
    time_ = FakeTime()

    results = list(
        downloader(lambda _: next(answers), holds=held(time_)).fetch([job(GITHUB_URL)], 1)
    )

    assert [r.status for r in results] == ["ok"]
    assert time_.pauses == [5]


@pytest.mark.parametrize(
    "past", ["Wed, 21 Oct 2015 07:28:00 GMT", "Wed, 21 Oct 2015 07:28:00 -0000"]
)
def test_rate_limited_download_reads_a_retry_after_date(past: str):
    answers = iter([httpx.Response(429, headers={"Retry-After": past}), httpx.Response(200)])
    time_ = FakeTime()

    results = list(
        downloader(lambda _: next(answers), holds=held(time_)).fetch([job(GITHUB_URL)], 1)
    )

    assert [r.status for r in results] == ["ok"]
    assert time_.pauses == []


def test_rate_limited_download_fails_after_three_retries():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "2"})

    time_ = FakeTime()
    results = list(downloader(handler, holds=held(time_)).fetch([job(GITHUB_URL)], 1))

    assert RATE_LIMIT_RETRIES == 3
    assert len(requests) == 4
    assert time_.pauses == [2, 2, 2]
    assert [(r.status, r.error) for r in results] == [("failed", "HTTP 429 Too Many Requests")]


def test_rate_limited_download_asked_to_wait_too_long_fails_without_waiting():
    too_long = str(MAX_RETRY_AFTER_S + 1)
    time_ = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": too_long})

    results = list(downloader(handler, holds=held(time_)).fetch([job(GITHUB_URL)], 1))

    assert MAX_RETRY_AFTER_S == 60
    assert time_.pauses == []
    assert [(r.status, r.error) for r in results] == [
        ("failed", "HTTP 429 Too Many Requests, asked to wait 61s")
    ]


def test_a_held_host_makes_its_other_downloads_wait_out_the_rest():
    time_ = FakeTime()
    holds = held(time_)
    cancel = threading.Event()

    holds.hold("opengraph.githubassets.com", 30)
    time_.now += 10
    assert holds.wait("opengraph.githubassets.com", cancel) == 0
    assert holds.wait("upload.wikimedia.org", cancel) == 0
    assert holds.wait("opengraph.githubassets.com", cancel) == 0

    assert time_.pauses == [20]


def test_a_later_shorter_hold_does_not_cut_a_longer_one():
    time_ = FakeTime()
    holds = held(time_)

    holds.hold("opengraph.githubassets.com", 30)
    holds.hold("opengraph.githubassets.com", 5)
    holds.wait("opengraph.githubassets.com", threading.Event())

    assert time_.pauses == [30]


def test_closing_fetch_stops_a_download_waiting_out_a_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/limited":
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, content=b"fast")

    jobs = [job("https://cdn.example.com/limited"), job("https://other.example.com/fast")]
    results = downloader(handler).fetch(jobs, 2)

    assert next(results).job.url.endswith("/fast")
    started = time.monotonic()
    results.close()

    assert time.monotonic() - started < 1.0


def test_a_host_that_asks_for_a_long_wait_is_not_tried_again_this_run():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "opengraph.githubassets.com":
            return httpx.Response(429, headers={"Retry-After": "900"})
        return httpx.Response(200, content=b"ok")

    jobs = [
        job("https://opengraph.githubassets.com/first"),
        job("https://opengraph.githubassets.com/second"),
        job("https://upload.wikimedia.org/third"),
    ]
    time_ = FakeTime()
    results = {
        r.job.url.rsplit("/", 1)[1]: r
        for r in downloader(handler, holds=held(time_)).fetch(jobs, 1)
    }

    assert sorted(requests) == [
        "https://opengraph.githubassets.com/first",
        "https://upload.wikimedia.org/third",
    ]
    assert time_.pauses == []
    assert (results["first"].status, results["first"].error) == (
        "failed",
        "HTTP 429 Too Many Requests, asked to wait 900s",
    )
    assert (results["second"].status, results["second"].error) == (
        "not_tried",
        "Not tried: opengraph.githubassets.com asked to wait 900s",
    )
    assert results["third"].status == "ok"


def test_a_hold_longer_than_the_most_slarc_waits_is_returned_without_pausing():
    time_ = FakeTime()
    holds = held(time_)

    holds.hold("opengraph.githubassets.com", 900)
    time_.now += 100

    assert holds.wait("opengraph.githubassets.com", threading.Event()) == 800
    assert time_.pauses == []
