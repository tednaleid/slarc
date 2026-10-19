# ABOUTME: Downloader over httpx that streams each URL into the blob store, several at a time.
# ABOUTME: Sends the session's token and d cookie only to Slack's private file host.
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

import httpx

from slarc.domain.links import is_slack_file_url
from slarc.ports import BlobStore, DownloadJob, DownloadResult, Session

FORBIDDEN_STATUSES = (403, 404)
MAX_REDIRECTS = 5

type FailedStatus = Literal["failed", "forbidden", "too_large"]


class TooLarge(Exception):
    pass


class HttpDownloader:
    def __init__(
        self, blobs: BlobStore, sessions: dict[str, Session], client: httpx.Client
    ) -> None:
        """sessions maps a team id to the session whose credentials fetch its files."""
        self._blobs = blobs
        self._sessions = sessions
        self._client = client

    def fetch(self, jobs: list[DownloadJob], parallelism: int) -> Iterator[DownloadResult]:
        """Yields each job's result as it finishes, with at most parallelism in flight."""
        pool = ThreadPoolExecutor(max_workers=parallelism)
        try:
            futures = [pool.submit(self._fetch, job) for job in jobs]
            for future in as_completed(futures):
                yield future.result()
        finally:
            # A caller that stops early, or Ctrl-C, abandons the downloads not started yet.
            pool.shutdown(cancel_futures=True)

    def _fetch(self, job: DownloadJob) -> DownloadResult:
        try:
            return self._download(job)
        except TooLarge:
            return _result(job, "too_large", f"Larger than the limit of {job.max_bytes} bytes.")
        except httpx.HTTPError as error:
            return _result(job, "failed", f"Download failed: {type(error).__name__}.")

    def _download(self, job: DownloadJob) -> DownloadResult:
        """Follows redirects itself, so each hop gets credentials only if it is a Slack file."""
        url = job.url
        for _ in range(MAX_REDIRECTS + 1):
            with self._client.stream("GET", url, headers=self._headers(job, url)) as response:
                if response.is_redirect and "location" in response.headers:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                if response.status_code in FORBIDDEN_STATUSES:
                    return _result(job, "forbidden", f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    return _result(job, "failed", f"HTTP {response.status_code}")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > job.max_bytes:
                    raise TooLarge
                sha256, size = self._blobs.write(_limited(response.iter_bytes(), job.max_bytes))
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                return DownloadResult(job, "ok", sha256, size, content_type or None, None)
        return _result(job, "failed", f"More than {MAX_REDIRECTS} redirects.")

    def _headers(self, job: DownloadJob, url: str) -> dict[str, str]:
        session = self._sessions.get(job.team_id)
        if not job.slack_auth or session is None or not is_slack_file_url(url):
            return {}
        return {"Authorization": f"Bearer {session.xoxc}", "Cookie": f"d={session.xoxd}"}


def _limited(chunks: Iterator[bytes], max_bytes: int) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise TooLarge
        yield chunk


def _result(job: DownloadJob, status: FailedStatus, error: str) -> DownloadResult:
    return DownloadResult(job, status, None, None, None, error)
