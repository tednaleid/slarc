# ABOUTME: Expiry: removes the messages past their retention, with the files and images only they
# ABOUTME: used, in one commit that conflicts if a tracked item or workspace setting changed.
from dataclasses import dataclass

from slarc.ports import BlobStore, CommitConflict, Reporter
from slarc.service.deletion import plan_deletion, stage
from slarc.service.remove import REMOVAL_ATTEMPTS
from slarc.unit_of_work import UnitOfWork

KEPT_CHANGING = "Other slarc commands kept changing the archive, so nothing was expired this time."


@dataclass(frozen=True)
class Expiry:
    """How many messages, files and images expiry removed."""

    messages: int = 0
    files: int = 0
    images: int = 0

    @property
    def anything(self) -> bool:
        return bool(self.messages or self.files or self.images)


def expire(uow: UnitOfWork, teams: list[str], blobs: BlobStore, reporter: Reporter) -> Expiry:
    """Commits the expiry of the teams that still have tracked items, and counts it in the run.

    Each attempt reads the tracked items and settings afresh and commits only if none of the
    teams' items or settings changed meanwhile; after REMOVAL_ATTEMPTS conflicts it warns and
    leaves the expiry to a later run. Download states it deletes leave run.downloads.
    """
    archive = uow.archive
    for _ in range(REMOVAL_ATTEMPTS):
        tracked = archive.tracked_items()
        live = [team for team in teams if any(i.team_id == team for i in tracked.values())]
        held = {key: item for key, item in tracked.items() if item.team_id in live}
        settings = archive.workspace_settings()
        deletion = plan_deletion(live, held, settings, uow.clock.now(), archive, blobs)
        stage(deletion, archive)
        try:
            uow.commit(
                unchanged=list(held.values()),
                complete_teams=live,
                unchanged_settings=[settings[team] for team in live if team in settings],
            )
        except CommitConflict:
            continue
        uow.forget_downloads(deletion.downloads)
        result = Expiry(deletion.messages, deletion.files, deletion.images)
        uow.run.counts["messages_expired"] = result.messages
        uow.run.counts["files_expired"] = result.files
        uow.run.counts["images_expired"] = result.images
        return result
    reporter.warn(KEPT_CHANGING)
    return Expiry()
