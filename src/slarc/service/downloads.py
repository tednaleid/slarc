# ABOUTME: Queues downloads of the files and images messages reference, then drains the queue.
# ABOUTME: Each result commits a directory line with meta.blob or a download state change.
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

import msgspec

from slarc.domain.directory import DirectoryView, directory_image_url
from slarc.domain.emoji import custom_emoji_url, shortcodes
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.hashing import content_hash
from slarc.domain.links import is_slack_file_url, is_web_url
from slarc.domain.tracking import DownloadState, TrackedItem
from slarc.ports import (
    ArchiveStore,
    AuthFailed,
    CommitConflict,
    DirectoryState,
    Downloader,
    DownloadJob,
    DownloadKey,
    DownloadResult,
    FileDeleted,
    NoSession,
    Reporter,
    SlackConnector,
    SlarcError,
)
from slarc.service.appends import append_directory, append_line, stamped_meta
from slarc.service.selectors import covered_stamps, message_versions
from slarc.unit_of_work import UnitOfWork

TERMINAL_STATUSES = ("forbidden", "not_found", "deleted", "external", "gave_up")
QUEUED_STATUSES = ("pending", "failed")
type ImageType = Literal["image", "emoji", "avatar"]
SKIPPED_STATUSES = ("too_large", "forbidden", "not_found", "gave_up")
OUTSIDE_MAX_ATTEMPTS = 3
DOWNLOAD_API = "download"
FILES_INFO_API = "files.info"
DIRECTORY_LIST_APIS: dict[Kind, str] = {"user": "users.list", "emoji": "emoji.list"}

type SettledStatus = Literal["failed", "forbidden", "not_found", "too_large", "deleted", "gave_up"]


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

    The next commit requires each file and image found downloaded to still be stored. The
    custom emoji and author avatars it shows are found through view. files, images and
    known, the archive's file, image and download state, are read here when not given.
    retry_skipped queues skipped downloads afresh, as requeue_skipped describes.
    """
    referenced = [file for file in message.get("files") or [] if file.get("id")]
    urls = list(image_types(message, view))
    if not referenced and not urls:
        return []
    stored = uow.archive.directory_state(team_id, "file") if files is None else files
    if images is None:
        images = uow.archive.directory_state(team_id, "image")
    known = uow.archive.downloads() if known is None else known
    queued = {(d.team_id, d.target) for d in uow.run.downloads}
    states = []
    for file in referenced:
        if _downloaded(uow, stored.get(file["id"])):
            uow.require((team_id, "file", file["id"]))
        else:
            prior = known.get((team_id, file["id"]))
            states.append(_file_state(file, team_id, max_bytes, prior, retry_skipped))
    for url in urls:
        if _downloaded(uow, images.get(url)):
            uow.require((team_id, "image", url))
        else:
            states.append(_image_state(url, team_id, known.get((team_id, url)), retry_skipped))
    jobs = []
    for state in states:
        if state is None or (state.team_id, state.target) in queued:
            continue
        uow.run.downloads.append(state)
        queued.add((state.team_id, state.target))
        if state.status == "pending":
            jobs.append(_job(state, max_bytes))
    return jobs


def requeue_skipped(uow: UnitOfWork, items: Sequence[TrackedItem], max_bytes: int) -> None:
    """Queues afresh, with attempts reset, the skipped downloads the items' stored messages
    reference, and commits them, without reading anything from Slack.

    Skipped means forbidden, not_found, gave_up, or an image that was too_large; a file that is
    too large is retried once its size fits.
    """
    archive = uow.archive
    known = archive.downloads()
    places: dict[tuple[str, str], list[TrackedItem]] = {}
    for item in items:
        places.setdefault((item.team_id, item.channel), []).append(item)
    views: dict[str, DirectoryView] = {}
    for (team_id, channel), held in places.items():
        if team_id not in views:
            views[team_id] = stored_directory(archive, team_id)
        files = archive.directory_state(team_id, "file")
        images = archive.directory_state(team_id, "image")
        versions = message_versions(archive.message_lines(team_id, channel))
        for ts in covered_stamps(versions, held):
            message = versions[ts][-1].object
            if message:
                queue_downloads(
                    message,
                    team_id,
                    uow,
                    max_bytes,
                    view=views[team_id],
                    files=files,
                    images=images,
                    known=known,
                    retry_skipped=True,
                )
    uow.commit()


def stored_directory(archive: ArchiveStore, team_id: str) -> DirectoryView:
    """The archive's users, conversations and custom emoji for a workspace."""
    emoji = archive.directory_objects(team_id, "emoji")
    return DirectoryView(
        users=archive.directory_objects(team_id, "user"),
        conversations=archive.directory_objects(team_id, "conversation"),
        usergroups={},
        emoji={name: obj.get("url", "") for name, obj in emoji.items()},
        bots={},
    )


def drain_downloads(
    uow: UnitOfWork,
    teams: Collection[str],
    downloader: Downloader,
    parallelism: int,
    max_bytes: int,
    connector: SlackConnector,
    reporter: Reporter,
) -> DrainedDownloads:
    """Downloads this run's pending targets and the teams' earlier queue, committing each result.

    The reporter hears how many downloads are done, failed and skipped before the first result
    and after each one; a target this run skipped without a job, such as a file too large to
    fetch, counts as done and skipped from the start. A result counts as failed or skipped by
    the status it settles into, so a file whose files.info read fails after its download
    counts as failed. A result whose target's download state an rm deleted meanwhile is
    dropped and counts as neither.
    Requeued downloads are committed before any job starts, so each later commit carries
    only its own result's state.
    """
    if _requeue_earlier(uow, teams, max_bytes):
        uow.commit(downloads_exist=True)
    jobs = [_job(state, max_bytes) for state in uow.run.downloads if state.status == "pending"]
    jobs = _with_credentials(jobs, connector, reporter)
    recorder = _Recorder(uow, connector, reporter)
    settled = sum(1 for state in uow.run.downloads if state.status in SKIPPED_STATUSES)
    if jobs or settled:
        done = skipped = settled
        failed = 0
        total = settled + len(jobs)
        reporter.downloads(done, failed, skipped, total)
        try:
            for result in downloader.fetch(jobs, parallelism):
                status = recorder.record(result)
                done += 1
                if status in QUEUED_STATUSES:
                    failed += 1
                elif status in SKIPPED_STATUSES:
                    skipped += 1
                reporter.downloads(done, failed, skipped, total)
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


def image_types(message: dict, view: DirectoryView | None) -> dict[str, ImageType]:
    """The message's images plus, through view, its custom emoji and its authors' avatars, each
    URL with which of those it is."""
    images: dict[str, ImageType] = dict.fromkeys(image_urls(message), "image")
    if view is not None:
        for name in _emoji_names(message):
            url = custom_emoji_url(name, view.emoji)
            if is_web_url(url):
                images.setdefault(url, "emoji")
        authors = [message.get("user"), *(message.get("reply_users") or [])]
        for user_id in authors:
            url = directory_image_url("user", view.users.get(user_id or "") or {})
            if url is not None:
                images.setdefault(url, "avatar")
    return images


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
    file: dict, team_id: str, max_bytes: int, prior: DownloadState | None, retry_skipped: bool
) -> DownloadState | None:
    """The state to record for a referenced file, or None when there is nothing to record.

    With retry_skipped, a file that was forbidden or not_found starts over.
    """
    if retry_skipped and prior is not None and prior.status in ("forbidden", "not_found"):
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

    With retry_skipped, an image that was skipped starts over.
    """
    if retry_skipped and prior is not None and prior.status in SKIPPED_STATUSES:
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


def _requeue_earlier(uow: UnitOfWork, teams: Collection[str], max_bytes: int) -> bool:
    """Queues the teams' pending and failed downloads from earlier runs, and their too_large
    files that now fit; returns whether it queued any."""
    queued = {(d.team_id, d.target) for d in uow.run.downloads}
    requeued = False
    sizes: dict[str, dict[str, dict]] = {}
    for key, state in outstanding_downloads(uow.archive).items():
        if key in queued or state.team_id not in teams or state.url is None:
            continue
        retry = state.status in QUEUED_STATUSES
        if state.status == "too_large" and state.target_kind == "file":
            if state.team_id not in sizes:
                sizes[state.team_id] = uow.archive.directory_objects(state.team_id, "file")
            size = sizes[state.team_id].get(state.target, {}).get("size")
            retry = size is not None and size <= max_bytes
        if retry:
            uow.run.downloads.append(msgspec.structs.replace(state, status="pending"))
            requeued = True
    return requeued


class _Recorder:
    """Records and commits each download result in the archive and the run's download states.

    A downloaded file is recorded with its files.info object. Users and emoji whose image
    was downloaded get their new lines in finish, which reads their directory once. saved
    counts the files and images committed with their blob.
    """

    def __init__(self, uow: UnitOfWork, connector: SlackConnector, reporter: Reporter) -> None:
        self.uow = uow
        self.connector = connector
        self.reporter = reporter
        self.images: dict[str, set[str]] = {}
        self.saved: dict[Kind, int] = {"file": 0, "image": 0}

    def record(self, result: DownloadResult) -> str | None:
        """Records and commits the result; returns the status it settled into, or None when it
        was downloaded or dropped.

        The commit drops the result, directory line and state change alike, when its target
        has no download state left. A result whose state an earlier commit dropped is
        dropped without recording it.
        """
        job = result.job
        key = (job.team_id, job.target)
        if self._held(key) is None:
            return None
        downloaded = None
        if result.status == "not_tried":
            self._not_tried(job, result.error)
        elif result.status != "ok":
            self._settle(job, result.status, result.error)
        elif job.target_kind == "file":
            downloaded = self._record_file(result)
        else:
            downloaded = self._record_image(result)
        if key in self.uow.commit(downloads_exist=True):
            return None
        if downloaded is not None:
            self.saved[downloaded] += 1
            if downloaded == "image":
                self.images.setdefault(job.team_id, set()).add(job.url)
        held = self._held(key)
        return held.status if held is not None else None

    def finish(self) -> None:
        """Commits the new lines of users and emoji whose image was downloaded, one workspace
        at a time, then what is left open. A workspace removed meanwhile makes its commit
        conflict, so its lines are discarded with a warning."""
        for team_id, urls in self.images.items():
            for kind, api in DIRECTORY_LIST_APIS.items():
                objects = self.uow.archive.directory_objects(team_id, kind)
                shown = {
                    id_: obj
                    for id_, obj in objects.items()
                    if directory_image_url(kind, obj) in urls
                }
                append_directory(self.uow, team_id, kind, shown, api)
            try:
                self.uow.commit(downloads_exist=True)
            except CommitConflict:
                self.reporter.warn(
                    f"Workspace {team_id} was removed while this pull was running, so its "
                    "avatars and emoji were not linked."
                )
        self.uow.commit(downloads_exist=True)

    def _record_file(self, result: DownloadResult) -> Kind | None:
        """Appends the downloaded file's line and returns "file", or settles the job and
        returns None when its files.info read fails."""
        job = result.job
        try:
            info = self.connector.connect(job.team_id).file_info(job.target)
        except FileDeleted as error:
            self._settle(job, "deleted", str(error))
            return None
        except AuthFailed:
            raise
        except SlarcError as error:
            self._settle(job, "failed", str(error))
            return None
        digest = content_hash("file", info)
        self._append(job, "file", info, FILES_INFO_API, digest, result.sha256)
        return "file"

    def _record_image(self, result: DownloadResult) -> Kind:
        job = result.job
        image = {"url": job.url, "content_type": result.content_type, "size": result.size}
        digest = content_hash("image", image)
        self._append(job, "image", image, DOWNLOAD_API, digest, result.sha256)
        return "image"

    def _append(
        self, job: DownloadJob, kind: Kind, obj: dict, api: str, digest: str, blob: str | None
    ) -> None:
        uow = self.uow
        meta = stamped_meta(
            uow, kind, team=job.team_id, id=job.target, api=api, hash=digest, blob=blob
        )
        append_line(uow, Envelope(meta=meta, object=obj))
        self._replace(job, None)

    def _held(self, key: DownloadKey) -> DownloadState | None:
        """The run's download state for the key, or None when it has none."""
        return next((d for d in self.uow.run.downloads if (d.team_id, d.target) == key), None)

    def _settle(self, job: DownloadJob, status: SettledStatus, error: str | None) -> None:
        held = self._held((job.team_id, job.target))
        if held is None:
            raise ValueError(f"No download state is queued for {job.target}")
        attempts = held.attempts + 1
        if status == "failed" and not job.slack_auth and attempts >= OUTSIDE_MAX_ATTEMPTS:
            status = "gave_up"
        settled = msgspec.structs.replace(held, status=status, attempts=attempts, last_error=error)
        self._replace(job, settled)

    def _not_tried(self, job: DownloadJob, error: str | None) -> None:
        """Keeps the job's status and attempts, recording only why it was not tried."""
        held = self._held((job.team_id, job.target))
        if held is None:
            raise ValueError(f"No download state is queued for {job.target}")
        self._replace(job, msgspec.structs.replace(held, last_error=error))

    def _replace(self, job: DownloadJob, state: DownloadState | None) -> None:
        """Replaces the run's state for the job's target; None settles it, as a download does,
        so the next commit deletes its download state."""
        key = (job.team_id, job.target)
        downloads = self.uow.run.downloads
        for index, held in enumerate(downloads):
            if (held.team_id, held.target) == key:
                if state is None:
                    del downloads[index]
                    self.uow.settle(key)
                else:
                    downloads[index] = state
                return
