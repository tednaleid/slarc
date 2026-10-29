# ABOUTME: Expiry: removes the messages past their retention, with the files and images only they
# ABOUTME: used, in one commit that conflicts if a tracked item or workspace setting changed.
from dataclasses import dataclass

from slarc.domain.tracking import retention_cutoff
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
    """Commits the expiry of the teams with a tracked item under a retention, and counts it in
    the run.

    Each attempt reads the tracked items and settings afresh and commits only if none of the
    teams' items or settings changed meanwhile; after REMOVAL_ATTEMPTS conflicts it warns and
    leaves the expiry to a later run. Download states it deletes leave run.downloads. The run's
    counts add up what each call expired.
    """
    archive = uow.archive
    for _ in range(REMOVAL_ATTEMPTS):
        now = uow.clock.now()
        tracked = archive.tracked_items()
        settings = archive.workspace_settings()
        live = [
            team
            for team in teams
            if any(
                retention_cutoff(item, settings, now) is not None
                for item in tracked.values()
                if item.team_id == team
            )
        ]
        held = {key: item for key, item in tracked.items() if item.team_id in live}
        deletion = plan_deletion(live, held, settings, now, archive, blobs, sweep=False)
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
        counts = uow.run.counts
        for name, count in (
            ("messages_expired", result.messages),
            ("files_expired", result.files),
            ("images_expired", result.images),
        ):
            held = counts.get(name, 0)
            counts[name] = (held if isinstance(held, int) else 0) + count
        return result
    reporter.warn(KEPT_CHANGING)
    return Expiry()


def expiry_line(expiry: Expiry, workspace: str) -> str:
    """Says what expiry removed from the workspace, naming only the kinds it removed."""
    parts = [
        _count(count, noun)
        for count, noun in (
            (expiry.messages, "message"),
            (expiry.files, "file"),
            (expiry.images, "image"),
        )
        if count
    ]
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return f"Expired {listed} from {workspace}."


def _count(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"
