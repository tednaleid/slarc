# ABOUTME: Plans and stages the deletion of what no tracked item keeps, shared by rm and expiry:
# ABOUTME: uncovered or expired message lines, the files and images only they used, and blobs.
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from slarc.domain.directory import directory_image_url
from slarc.domain.envelope import Envelope
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem, WorkspaceSetting
from slarc.ports import ArchiveStore, BlobStore, DownloadKey
from slarc.service.downloads import image_urls
from slarc.service.selectors import covered_stamps, message_versions


@dataclass(frozen=True)
class TeamDeletion:
    """One workspace's share of a deletion; a wiped workspace has nothing left tracked."""

    team: str
    wiped: bool
    stamps: dict[str, set[str]]
    lines: int
    files: set[str]
    images: set[str]
    downloads: list[DownloadKey]


@dataclass(frozen=True)
class Deletion:
    """What one commit deletes, and the blobs no directory row references once it commits."""

    teams: list[TeamDeletion]
    blobs: set[str]

    @property
    def messages(self) -> int:
        return sum(len(stamps) for team in self.teams for stamps in team.stamps.values())

    @property
    def lines(self) -> int:
        return sum(team.lines for team in self.teams)

    @property
    def files(self) -> int:
        return sum(len(team.files) for team in self.teams)

    @property
    def images(self) -> int:
        return sum(len(team.images) for team in self.teams)

    @property
    def downloads(self) -> set[DownloadKey]:
        return {key for team in self.teams for key in team.downloads}


def plan_deletion(
    teams: list[str],
    tracked: dict[ItemKey, TrackedItem],
    settings: Mapping[str, WorkspaceSetting],
    now: datetime,
    archive: ArchiveStore,
    blobs: BlobStore,
    *,
    sweep: bool,
) -> Deletion:
    """What the teams lose when tracked holds the items still tracked, under the retention in
    effect at now.

    With sweep, every file and image no kept message references goes, as rm needs; without it,
    only those the deleted messages referenced, so expiry leaves earlier avatars and the like.
    """
    known = archive.downloads()
    deletions = [
        _team_deletion(
            team,
            [i for i in tracked.values() if i.team_id == team],
            settings,
            now,
            known,
            archive,
            sweep,
        )
        for team in teams
    ]
    dropped = set()
    wiped = set()
    for team in deletions:
        if team.wiped:
            wiped.add(team.team)
        dropped |= {(team.team, "file", id_) for id_ in team.files}
        dropped |= {(team.team, "image", id_) for id_ in team.images}
    referenced = {
        blob
        for team, kind, id_, blob in archive.blob_references()
        if team not in wiped and (team, kind, id_) not in dropped
    }
    return Deletion(deletions, set(blobs.ids()) - referenced)


def stage(deletion: Deletion, archive: ArchiveStore) -> None:
    """Stages the drops of the deletion's message lines, files, images and download states."""
    for team in deletion.teams:
        if team.wiped:
            archive.drop_workspace(team.team)
        else:
            for channel, stamps in team.stamps.items():
                archive.drop_messages(team.team, channel, stamps)
            if team.files:
                archive.drop_directory_lines(team.team, "file", team.files)
            if team.images:
                archive.drop_directory_lines(team.team, "image", team.images)
        if team.downloads:
            archive.drop_downloads(team.team, {target for _, target in team.downloads})


def _team_deletion(
    team: str,
    tracked: list[TrackedItem],
    settings: Mapping[str, WorkspaceSetting],
    now: datetime,
    known: dict[DownloadKey, DownloadState],
    archive: ArchiveStore,
    sweep: bool,
) -> TeamDeletion:
    """Message lines no item keeps, then the files and images no kept message line references:
    any such, with sweep, or else only those the deleted lines referenced.

    Avatars and custom emoji images stay while anything in the workspace is tracked.
    """
    stamps: dict[str, set[str]] = {}
    lines = 0
    file_refs: set[str] = set()
    image_refs: set[str] = set()
    dropped_files: set[str] = set()
    dropped_images: set[str] = set()
    for channel in archive.channels(team):
        versions = message_versions(archive.message_lines(team, channel))
        held = [item for item in tracked if item.channel == channel]
        covered = covered_stamps(versions, held, settings, now)
        uncovered = set(versions) - covered
        if uncovered:
            stamps[channel] = uncovered
            lines += sum(len(versions[ts]) for ts in uncovered)
        for ts in covered:
            _add_references(versions[ts], file_refs, image_refs)
        for ts in uncovered:
            _add_references(versions[ts], dropped_files, dropped_images)
    if tracked:
        image_refs |= _directory_images(team, archive)
    stored_files = set(archive.directory_state(team, "file"))
    stored_images = set(archive.directory_state(team, "image"))
    if sweep:
        dropped_files = stored_files | _download_targets(known, team, "file")
        dropped_images = stored_images | _download_targets(known, team, "image")
    unused_files = dropped_files - file_refs
    unused_images = dropped_images - image_refs
    downloads = [
        key
        for key, state in known.items()
        if state.team_id == team
        and state.target in (unused_files if state.target_kind == "file" else unused_images)
    ]
    files = unused_files & stored_files
    images = unused_images & stored_images
    return TeamDeletion(team, not tracked, stamps, lines, files, images, downloads)


def _download_targets(
    known: dict[DownloadKey, DownloadState], team: str, kind: Literal["file", "image"]
) -> set[str]:
    return {s.target for s in known.values() if (s.team_id, s.target_kind) == (team, kind)}


def _add_references(lines: list[Envelope], files: set[str], images: set[str]) -> None:
    for env in lines:
        message = env.object or {}
        files.update(f["id"] for f in message.get("files") or [] if f.get("id"))
        images.update(image_urls(message))


def _directory_images(team: str, archive: ArchiveStore) -> set[str]:
    """The current avatar and custom emoji image URLs of the workspace's directory."""
    urls = set()
    for kind in ("user", "emoji"):
        for obj in archive.directory_objects(team, kind).values():
            url = directory_image_url(kind, obj)
            if url is not None:
                urls.add(url)
    return urls
