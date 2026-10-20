# ABOUTME: Queues downloads of the files and images messages reference, then drains the queue.
# ABOUTME: A download lands as a directory line with meta.blob; other outcomes as download states.
from dataclasses import dataclass
from typing import Literal

import msgspec

from slarc.domain.directory import DirectoryView, directory_image_url
from slarc.domain.emoji import custom_emoji_url, shortcodes
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.hashing import content_hash
from slarc.domain.links import is_slack_file_url, is_web_url
from slarc.domain.tracking import DownloadState
from slarc.ports import (
    ArchiveStore,
    AuthFailed,
    DirectoryState,
    Downloader,
    DownloadJob,
    DownloadResult,
    FileDeleted,
    NoSession,
    Reporter,
    SlackConnector,
    SlarcError,
)
from slarc.service.appends import append_directory, append_line, stamped_meta
from slarc.unit_of_work import UnitOfWork

TERMINAL_STATUSES = ("forbidden", "deleted", "external", "gave_up")
QUEUED_STATUSES = ("pending", "failed")
SKIPPED_STATUSES = ("too_large", "forbidden", "gave_up")
OUTSIDE_MAX_ATTEMPTS = 3
DOWNLOAD_API = "download"
FILES_INFO_API = "files.info"
DIRECTORY_LIST_APIS: dict[Kind, str] = {"user": "users.list", "emoji": "emoji.list"}

type DownloadKey = tuple[str, str]
type SettledStatus = Literal["failed", "forbidden", "too_large", "deleted", "gave_up"]


@dataclass(frozen=True)
class DrainedDownloads:
    """This run's files and images saved, and its downloads left queued or skipped."""

    files_saved: int
    images_saved: int
    pending: int
    skipped: int


def image_urls(message: dict) -> list[str]:
    """The images a message shows: layout block images, link preview images and app icons."""
    urls: list[object] = []
    for block in message.get("blocks") or []:
        if block.get("type") == "image":
            urls.append(block.get("image_url"))
        elif block.get("type") == "context":
            elements = block.get("elements") or []
            urls.extend(e.get("image_url") for e in elements if e.get("type") == "image")
    for attachment in message.get("attachments") or []:
        urls.extend(attachment.get(key) for key in ("image_url", "thumb_url", "service_icon"))
    urls.extend(((message.get("bot_profile") or {}).get("icons") or {}).values())
    return list(dict.fromkeys(url for url in urls if is_web_url(url)))


def queue_downloads(
    message: dict,
    team_id: str,
    uow: UnitOfWork,
    max_bytes: int,
    *,
    view: DirectoryView | None = None,
    files: dict[str, DirectoryState] | None = None,
    images: dict[str, DirectoryState] | None = None,
    known: dict[DownloadKey, DownloadState] | None = None,
    retry_skipped: bool = False,
) -> list[DownloadJob]:
    """Queues the message's files and images that are neither downloaded nor settled.

    The custom emoji and author avatars it shows are found through view. files, images and
    known, the archive's file, image and download state, are read here when not given.
    retry_skipped queues forbidden and gave_up outside-site images afresh.
    """
    referenced = message.get("files") or []
    urls = _shown_images(message, view)
    if not referenced and not urls:
        return []
    stored = uow.archive.directory_state(team_id, "file") if files is None else files
    if images is None:
        images = uow.archive.directory_state(team_id, "image")
    known = uow.archive.downloads() if known is None else known
    queued = {(d.team_id, d.target) for d in uow.run.downloads}
    states = [
        _file_state(file, team_id, max_bytes, known.get((team_id, file["id"])))
        for file in referenced
        if not _downloaded(uow, stored.get(file["id"]))
    ]
    states += [
        _image_state(url, team_id, known.get((team_id, url)), retry_skipped)
        for url in urls
        if not _downloaded(uow, images.get(url))
    ]
    jobs = []
    for state in states:
        if state is None or (state.team_id, state.target) in queued:
            continue
        uow.run.downloads.append(state)
        queued.add((state.team_id, state.target))
        if state.status == "pending":
            jobs.append(_job(state, max_bytes))
    return jobs


def drain_downloads(
    uow: UnitOfWork,
    downloader: Downloader,
    parallelism: int,
    max_bytes: int,
    connector: SlackConnector,
    reporter: Reporter,
) -> DrainedDownloads:
    """Downloads this run's pending targets and earlier runs' queue, recording each result.

    The reporter hears how many jobs are done, failed and skipped before the first result and
    after each one. A result counts as failed or skipped by the status it settles into, so a
    file whose files.info read fails after its download counts as failed.
    """
    _requeue_earlier(uow, max_bytes)
    jobs = [_job(state, max_bytes) for state in uow.run.downloads if state.status == "pending"]
    jobs = _with_credentials(jobs, connector, reporter)
    recorder = _Recorder(uow, connector)
    if jobs:
        done = failed = skipped = 0
        reporter.downloads(done, failed, skipped, len(jobs))
        try:
            for result in downloader.fetch(jobs, parallelism):
                status = recorder.record(result)
                done += 1
                if status in QUEUED_STATUSES:
                    failed += 1
                elif status in SKIPPED_STATUSES:
                    skipped += 1
                reporter.downloads(done, failed, skipped, len(jobs))
        finally:
            recorder.finish()
    states = uow.run.downloads
    return DrainedDownloads(
        files_saved=recorder.saved["file"],
        images_saved=recorder.saved["image"],
        pending=sum(1 for state in states if state.status in QUEUED_STATUSES),
        skipped=sum(1 for state in states if state.status in SKIPPED_STATUSES),
    )


def _with_credentials(
    jobs: list[DownloadJob], connector: SlackConnector, reporter: Reporter
) -> list[DownloadJob]:
    """The jobs to fetch, connecting each workspace whose Slack files need its credentials.

    A workspace that cannot connect keeps its Slack file jobs queued, untried, with one warning.
    """
    unreachable = set()
    for team_id in dict.fromkeys(job.team_id for job in jobs if job.slack_auth):
        try:
            connector.connect(team_id)
        except (NoSession, AuthFailed) as error:
            reporter.warn(
                f"Could not connect to workspace {team_id}, so its queued file downloads wait "
                f"for a later pull: {error}"
            )
            unreachable.add(team_id)
    return [job for job in jobs if not (job.slack_auth and job.team_id in unreachable)]


def outstanding_downloads(archive: ArchiveStore) -> dict[DownloadKey, DownloadState]:
    """The latest download state of each target whose directory line has no blob.

    A downloaded target drops out of its run's list, so an earlier run's entry stays its
    latest state; the blob on the directory line is what records the download.
    """
    lines: dict[tuple[str, Kind], dict[str, DirectoryState]] = {}
    outstanding = {}
    for key, state in archive.downloads().items():
        kind_key = (state.team_id, state.target_kind)
        if kind_key not in lines:
            lines[kind_key] = archive.directory_state(*kind_key)
        held = lines[kind_key].get(state.target)
        if held is None or held.blob is None:
            outstanding[key] = state
    return outstanding


def _shown_images(message: dict, view: DirectoryView | None) -> list[str]:
    """The message's images plus, through view, its custom emoji and its authors' avatars."""
    urls = image_urls(message)
    if view is not None:
        for name in _emoji_names(message):
            url = custom_emoji_url(name, view.emoji)
            if is_web_url(url):
                urls.append(url)
        authors = [message.get("user"), *(message.get("reply_users") or [])]
        for user_id in authors:
            url = directory_image_url("user", view.users.get(user_id or "") or {})
            if url is not None:
                urls.append(url)
    return list(dict.fromkeys(urls))


def _emoji_names(message: dict) -> list[str]:
    """Emoji in reactions, rich_text blocks and mrkdwn text, without skin tones."""
    names = [reaction["name"] for reaction in message.get("reactions") or []]
    names += _block_emoji(message.get("blocks") or [])
    texts = [message.get("text")]
    texts += [attachment.get("text") for attachment in message.get("attachments") or []]
    names += [name for text in texts if isinstance(text, str) for name in shortcodes(text)]
    return list(dict.fromkeys(name.split("::")[0] for name in names))


def _block_emoji(node: object) -> list[str]:
    if isinstance(node, list):
        return [name for item in node for name in _block_emoji(item)]
    if not isinstance(node, dict):
        return []
    names = [node["name"]] if node.get("type") == "emoji" and node.get("name") else []
    return names + [name for value in node.values() for name in _block_emoji(value)]


def _file_state(
    file: dict, team_id: str, max_bytes: int, prior: DownloadState | None
) -> DownloadState | None:
    """The state to record for a referenced file, or None when there is nothing to record."""
    if prior is not None and prior.status == "removed":
        prior = None
    if prior is not None and prior.status in TERMINAL_STATUSES:
        return None
    url = file.get("url_private_download") or file.get("url_private")
    state = DownloadState(
        team_id=team_id,
        target_kind="file",
        target=file["id"],
        url=url,
        status="pending",
        attempts=prior.attempts if prior is not None else 0,
    )
    mode = file.get("mode")
    if mode == "tombstone":
        return msgspec.structs.replace(state, status="deleted")
    if mode == "external":
        return msgspec.structs.replace(state, status="external")
    if mode == "hidden_by_limit" or not url:
        return None
    if (file.get("size") or 0) > max_bytes:
        if prior is not None and prior.status == "too_large":
            return None
        return msgspec.structs.replace(state, status="too_large")
    return state


def _image_state(
    url: str, team_id: str, prior: DownloadState | None, retry_skipped: bool
) -> DownloadState | None:
    """An image over the size limit stays too_large: its size is known only by fetching it.

    With retry_skipped, an outside-site image that was forbidden or gave_up starts over.
    """
    if prior is not None and prior.status == "removed":
        prior = None
    if (
        retry_skipped
        and prior is not None
        and prior.status in ("forbidden", "gave_up")
        and not is_slack_file_url(url)
    ):
        prior = None
    if prior is not None and prior.status in (*TERMINAL_STATUSES, "too_large"):
        return None
    attempts = prior.attempts if prior is not None else 0
    return DownloadState(
        team_id=team_id,
        target_kind="image",
        target=url,
        url=url,
        status="pending",
        attempts=attempts,
    )


def _downloaded(uow: UnitOfWork, held: DirectoryState | None) -> bool:
    return held is not None and held.blob is not None and uow.blobs.exists(held.blob)


def _job(state: DownloadState, max_bytes: int) -> DownloadJob:
    url = state.url or ""
    slack_auth = is_slack_file_url(url)
    return DownloadJob(state.team_id, state.target_kind, state.target, url, max_bytes, slack_auth)


def _requeue_earlier(uow: UnitOfWork, max_bytes: int) -> None:
    """Queues earlier runs' pending and failed downloads, and too_large files that now fit."""
    queued = {(d.team_id, d.target) for d in uow.run.downloads}
    sizes: dict[str, dict[str, dict]] = {}
    for key, state in outstanding_downloads(uow.archive).items():
        if key in queued or state.url is None:
            continue
        retry = state.status in QUEUED_STATUSES
        if state.status == "too_large" and state.target_kind == "file":
            if state.team_id not in sizes:
                sizes[state.team_id] = uow.archive.directory_objects(state.team_id, "file")
            size = sizes[state.team_id].get(state.target, {}).get("size")
            retry = size is not None and size <= max_bytes
        if retry:
            uow.run.downloads.append(msgspec.structs.replace(state, status="pending"))


class _Recorder:
    """Records each download result in the archive and the run's download states.

    A downloaded file is recorded with its files.info object. Users and emoji whose image
    was downloaded get their new lines in finish, which reads their directory once. saved
    counts the files and images recorded with their blob.
    """

    def __init__(self, uow: UnitOfWork, connector: SlackConnector) -> None:
        self.uow = uow
        self.connector = connector
        self.images: dict[str, set[str]] = {}
        self.saved: dict[Kind, int] = {"file": 0, "image": 0}

    def record(self, result: DownloadResult) -> str | None:
        """Records the result; returns the status it settled into, or None when downloaded."""
        job = result.job
        if result.status != "ok":
            self._settle(job, result.status, result.error)
        elif job.target_kind == "file":
            self._record_file(result)
        else:
            self._record_image(result)
        self.uow.archive.flush()
        key = (job.team_id, job.target)
        held = (d for d in self.uow.run.downloads if (d.team_id, d.target) == key)
        return next((d.status for d in held), None)

    def finish(self) -> None:
        for team_id, urls in self.images.items():
            for kind, api in DIRECTORY_LIST_APIS.items():
                objects = self.uow.archive.directory_objects(team_id, kind)
                shown = {
                    id_: obj
                    for id_, obj in objects.items()
                    if directory_image_url(kind, obj) in urls
                }
                append_directory(self.uow, team_id, kind, shown, api)

    def _record_file(self, result: DownloadResult) -> None:
        job = result.job
        try:
            info = self.connector.connect(job.team_id).file_info(job.target)
        except FileDeleted as error:
            self._settle(job, "deleted", str(error))
            return
        except AuthFailed:
            raise
        except SlarcError as error:
            self._settle(job, "failed", str(error))
            return
        digest = content_hash("file", info)
        self._append(job, "file", info, FILES_INFO_API, digest, result.sha256)

    def _record_image(self, result: DownloadResult) -> None:
        job = result.job
        image = {"url": job.url, "content_type": result.content_type, "size": result.size}
        digest = content_hash("image", image)
        self._append(job, "image", image, DOWNLOAD_API, digest, result.sha256)
        self.images.setdefault(job.team_id, set()).add(job.url)

    def _append(
        self, job: DownloadJob, kind: Kind, obj: dict, api: str, digest: str, blob: str | None
    ) -> None:
        uow = self.uow
        meta = stamped_meta(
            uow, kind, team=job.team_id, id=job.target, api=api, hash=digest, blob=blob
        )
        append_line(uow, Envelope(meta=meta, object=obj))
        self.saved[kind] += 1
        self._replace(job, None)

    def _settle(self, job: DownloadJob, status: SettledStatus, error: str | None) -> None:
        key = (job.team_id, job.target)
        held = next(d for d in self.uow.run.downloads if (d.team_id, d.target) == key)
        attempts = held.attempts + 1
        if status == "failed" and not job.slack_auth and attempts >= OUTSIDE_MAX_ATTEMPTS:
            status = "gave_up"
        settled = msgspec.structs.replace(held, status=status, attempts=attempts, last_error=error)
        self._replace(job, settled)

    def _replace(self, job: DownloadJob, state: DownloadState | None) -> None:
        """Replaces the run's state for the job's target; None drops it, as a download does."""
        key = (job.team_id, job.target)
        downloads = self.uow.run.downloads
        for index, held in enumerate(downloads):
            if (held.team_id, held.target) == key:
                if state is None:
                    del downloads[index]
                else:
                    downloads[index] = state
                return
