# ABOUTME: Downloader over httpx that streams each URL into the blob store, several at a time.
# ABOUTME: Sends the session's token and d cookie only to Slack's private file host.
import threading
from collections.abc import Generator, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

import httpx

from slarc.domain.links import is_slack_file_url
from slarc.ports import BlobStore, DownloadJob, DownloadResult, Session

FORBIDDEN_STATUSES = (403, 404)
MAX_REDIRECTS = 5
SESSION_HINT = "The Slack session may have expired; sign in again and pull to retry."

type FailedStatus = Literal["failed", "forbidden", "too_large"]


class TooLarge(Exception):
    pass


class Cancelled(Exception):
    pass


class HttpDownloader:
    def __init__(
        self, blobs: BlobStore, sessions: dict[str, Session], client: httpx.Client
    ) -> None:
        """sessions maps a team id to the session whose credentials fetch its files."""
        self._blobs = blobs
        self._sessions = sessions
        self._client = client

    def fetch(self, jobs: list[DownloadJob], parallelism: int) -> Generator[DownloadResult]:
        """Yields each job's result as it finishes, with at most parallelism in flight."""
        pool = ThreadPoolExecutor(max_workers=parallelism)
        cancel = threading.Event()
        try:
            futures = [pool.submit(self._fetch, job, cancel) for job in jobs]
            for future in as_completed(futures):
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
        """Follows redirects itself, so each hop gets credentials only if it is a Slack file."""
        url = job.url
        for _ in range(MAX_REDIRECTS + 1):
            _raise_if_set(cancel)
            with self._client.stream("GET", url, headers=self._headers(job, url)) as response:
                if response.is_redirect and "location" in response.headers:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code in FORBIDDEN_STATUSES:
                    return _result(job, "forbidden", f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    return _result(job, "failed", f"HTTP {response.status_code}")
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
