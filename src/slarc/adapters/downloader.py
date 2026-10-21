# ABOUTME: Downloader over httpx that streams each URL into the blob store, several at a time,
# ABOUTME: pacing each host and sending the session's credentials only to Slack's file host.
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlparse

import httpx

from slarc.domain.links import SLACK_FILE_HOST, is_slack_file_url
from slarc.ports import BlobStore, DownloadJob, DownloadResult, Session

MAX_REDIRECTS = 5
OUTSIDE_TIMEOUT_S = 15.0
OUTSIDE_HOST_PARALLELISM = 2
RATE_LIMIT_RETRIES = 3
DEFAULT_RETRY_AFTER_S = 5.0
MAX_RETRY_AFTER_S = 60
SESSION_HINT = "The Slack session may have expired; sign in again and pull to retry."

type FailedStatus = Literal["failed", "forbidden", "not_found", "too_large", "not_tried"]

TERMINAL_HTTP_STATUSES: dict[int, FailedStatus] = {403: "forbidden", 404: "not_found"}


class TooLarge(Exception):
    pass


class Cancelled(Exception):
    pass


def _pause(seconds: float, cancel: threading.Event) -> None:
    if cancel.wait(seconds):
        raise Cancelled


class HostHolds:
    """How long each host that answered HTTP 429 asked to be left alone, so every download from
    it waits out the rest of that time before its next request."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float, threading.Event], None] = _pause,
    ) -> None:
        self._clock = clock
        self._pause = pause
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def hold(self, host: str, seconds: float) -> None:
        with self._lock:
            self._until[host] = max(self._until.get(host, 0.0), self._clock() + seconds)

    def wait(self, host: str, cancel: threading.Event) -> float:
        """Pauses until the host's hold ends and returns 0, or returns the seconds left at once
        when more than MAX_RETRY_AFTER_S are. Raises Cancelled if cancel is set first."""
        with self._lock:
            left = self._until.get(host, 0.0) - self._clock()
        if left > MAX_RETRY_AFTER_S:
            return left
        if left > 0:
            self._pause(left, cancel)
        return 0


class HttpDownloader:
    def __init__(
        self,
        blobs: BlobStore,
        sessions: dict[str, Session],
        client: httpx.Client,
        holds: HostHolds | None = None,
    ) -> None:
        """sessions maps a team id to the session whose credentials fetch its files."""
        self._blobs = blobs
        self._sessions = sessions
        self._client = client
        self._holds = holds or HostHolds()

    def fetch(self, jobs: list[DownloadJob], parallelism: int) -> Generator[DownloadResult]:
        """Yields each job's result as it finishes, with at most parallelism in flight, and at
        most OUTSIDE_HOST_PARALLELISM of them from any host but Slack's file host."""
        pool = ThreadPoolExecutor(max_workers=parallelism)
        cancel = threading.Event()
        queued: dict[str, deque[DownloadJob]] = {}
        for job in jobs:
            queued.setdefault(_host(job.url), deque()).append(job)
        running: dict[Future[DownloadResult], str] = {}

        def start(host: str) -> None:
            if queued[host]:
                running[pool.submit(self._fetch, queued[host].popleft(), cancel)] = host

        try:
            for host in queued:
                for _ in range(_host_parallelism(host, parallelism)):
                    start(host)
            while running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    start(running.pop(future))
                    yield future.result()
        finally:
            # A caller that stops early, or Ctrl-C, abandons the downloads not started yet
            # and tells the running ones to stop at their next chunk.
            cancel.set()
            pool.shutdown(cancel_futures=True)

    def _fetch(self, job: DownloadJob, cancel: threading.Event) -> DownloadResult:
        try:
            return self._download(job, cancel)
        except Cancelled:
            return _result(job, "failed", "Download interrupted.")
        except TooLarge:
            return _result(job, "too_large", f"Larger than the limit of {job.max_bytes} bytes.")
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            return _result(job, "failed", f"Download failed: {type(error).__name__}.")

    def _download(self, job: DownloadJob, cancel: threading.Event) -> DownloadResult:
        """Follows redirects itself, so each hop gets credentials only if it is a Slack file.

        A job outside Slack's file host gets a shorter timeout than the client's default. An
        HTTP 429 holds its host for the Retry-After. Within MAX_RETRY_AFTER_S the download waits
        and retries, up to RATE_LIMIT_RETRIES times; past it, the download fails, and later
        requests to the host are not tried while the hold lasts.
        """
        url = job.url
        timeout = httpx.USE_CLIENT_DEFAULT if job.slack_auth else OUTSIDE_TIMEOUT_S
        redirects = 0
        retries = 0
        while redirects <= MAX_REDIRECTS:
            _raise_if_set(cancel)
            host = _host(url)
            left = self._holds.wait(host, cancel)
            if left:
                return _result(job, "not_tried", f"Not tried: {host} asked to wait {left:.0f}s")
            headers = self._headers(job, url)
            with self._client.stream("GET", url, headers=headers, timeout=timeout) as response:
                if response.is_redirect and "location" in response.headers:
                    url = str(response.url.join(response.headers["location"]))
                    redirects += 1
                    continue
                code = response.status_code
                if code == httpx.codes.TOO_MANY_REQUESTS:
                    seconds = _retry_after(response.headers.get("retry-after"))
                    self._holds.hold(host, seconds)
                    if seconds > MAX_RETRY_AFTER_S:
                        error = f"{_http_error(code)}, asked to wait {seconds:.0f}s"
                        return _result(job, "failed", error)
                    if retries == RATE_LIMIT_RETRIES:
                        return _result(job, "failed", _http_error(code))
                    retries += 1
                    continue
                if code in TERMINAL_HTTP_STATUSES:
                    return _result(job, TERMINAL_HTTP_STATUSES[code], _http_error(code))
                if code >= 400:
                    return _result(job, "failed", _http_error(code))
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                refusal = _slack_refusal(job, url, content_type)
                if refusal is not None:
                    return _result(job, "failed", refusal)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > job.max_bytes:
                    raise TooLarge
                sha256, size = self._blobs.write(
                    _limited(response.iter_bytes(), job.max_bytes, cancel)
                )
                return DownloadResult(job, "ok", sha256, size, content_type or None, None)
        return _result(job, "failed", f"More than {MAX_REDIRECTS} redirects.")

    def _headers(self, job: DownloadJob, url: str) -> dict[str, str]:
        session = self._sessions.get(job.team_id)
        if not job.slack_auth or session is None or not is_slack_file_url(url):
            return {}
        return {"Authorization": f"Bearer {session.xoxc}", "Cookie": f"d={session.xoxd}"}


def _slack_refusal(job: DownloadJob, url: str, content_type: str) -> str | None:
    """Why a response to a Slack file download is not the file, or None when it is.

    Slack answers an expired session by redirecting elsewhere or serving its sign-in page.
    """
    if not job.slack_auth:
        return None
    if not is_slack_file_url(url):
        return f"Slack redirected the file download away from its file host. {SESSION_HINT}"
    if content_type == "text/html":
        return f"Slack answered the file download with a web page. {SESSION_HINT}"
    return None


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _host_parallelism(host: str, parallelism: int) -> int:
    return parallelism if host == SLACK_FILE_HOST else min(parallelism, OUTSIDE_HOST_PARALLELISM)


def _retry_after(value: str | None) -> float:
    """Seconds to wait from a Retry-After header, given as seconds or as an HTTP date."""
    if value is None:
        return DEFAULT_RETRY_AFTER_S
    if value.strip().isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return DEFAULT_RETRY_AFTER_S
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _http_error(code: int) -> str:
    """The status code with its standard reason phrase, when it has one."""
    return f"HTTP {code} {httpx.codes.get_reason_phrase(code)}".rstrip()


def _raise_if_set(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled


def _limited(chunks: Iterator[bytes], max_bytes: int, cancel: threading.Event) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        _raise_if_set(cancel)
        total += len(chunk)
        if total > max_bytes:
            raise TooLarge
        yield chunk


def _result(job: DownloadJob, status: FailedStatus, error: str) -> DownloadResult:
    return DownloadResult(job, status, None, None, None, error)
