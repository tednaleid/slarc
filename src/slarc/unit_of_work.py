# ABOUTME: Unit of work scoping one run over the archive and blob stores. Commits the open batch
# ABOUTME: with the run's download changes; appends the run line however it ends, then maintains.
from collections.abc import Collection, Sequence
from types import TracebackType

import msgspec

from slarc.domain.envelope import fetched_at, run_id
from slarc.domain.tracking import (
    DownloadState,
    ItemKey,
    RunRecord,
    TrackedItem,
    WorkspaceSetting,
)
from slarc.ports import (
    ArchiveStore,
    BlobStore,
    Clock,
    Commit,
    CommitConflict,
    DirectoryRef,
    DownloadKey,
    Reporter,
)


class UnitOfWork:
    """maintained is None until the run's maintenance returns, then whether it deleted what
    nothing uses or left that for a later run because another command had the archive open."""

    run: RunRecord
    maintained: bool | None

    def __init__(
        self,
        archive: ArchiveStore,
        blobs: BlobStore,
        clock: Clock,
        command: str,
        args: list[str],
        slarc_version: str,
        run_suffix: str = "",
        reporter: Reporter | None = None,
    ) -> None:
        self.archive = archive
        self.blobs = blobs
        self.clock = clock
        self.command = command
        self.args = args
        self.slarc_version = slarc_version
        self.run_suffix = run_suffix
        self.reporter = reporter
        self._revisions: dict[ItemKey, int] = {}
        self._committed_downloads: list[DownloadState] = []
        self._settled: set[DownloadKey] = set()
        self._requires: set[DirectoryRef] = set()
        self.maintained = None

    def __enter__(self) -> UnitOfWork:
        now = self.clock.now()
        self.run = RunRecord(
            run=run_id(now, self.run_suffix),
            command=self.command,
            args=list(self.args),
            started=fetched_at(now),
        )
        self._committed_downloads = list(self.run.downloads)
        return self

    def commit(
        self,
        items: Sequence[TrackedItem] = (),
        *,
        unchanged: Sequence[TrackedItem] = (),
        downloads_exist: bool = False,
        complete_teams: Collection[str] = frozenset(),
        settings: Sequence[WorkspaceSetting] = (),
        unchanged_settings: Sequence[WorkspaceSetting] = (),
    ) -> set[DownloadKey]:
        """Commits the open batch with the items and settings, the download states in
        run.downloads that changed since the last commit, and the keys settled and refs
        required since then.

        Each item is sent with the revision this run last committed for its key, so a stale
        copy of an item commits. Unchanged items are sent as given, since they are read just
        before the commit to check nothing else changed them. Returns the download keys the
        commit dropped, which leave run.downloads. On CommitConflict the run is put back as it
        was at the last commit, and the conflict is raised.
        """
        committed_states = {_download_key(state): state for state in self._committed_downloads}
        change = Commit(
            items=[self._current(item) for item in items],
            unchanged=list(unchanged),
            downloads=[
                state
                for state in self.run.downloads
                if committed_states.get(_download_key(state)) != state
            ],
            settled=frozenset(self._settled),
            requires=frozenset(self._requires),
            downloads_exist=downloads_exist,
            complete_teams=frozenset(complete_teams),
            settings=settings,
            unchanged_settings=unchanged_settings,
        )
        try:
            committed = self.archive.commit(change)
        except CommitConflict:
            self.discard()
            raise
        self._revisions.update(committed.revisions)
        self.run.downloads[:] = [
            state for state in self.run.downloads if _download_key(state) not in committed.dropped
        ]
        self._committed_downloads = list(self.run.downloads)
        self._settled.clear()
        self._requires.clear()
        return committed.dropped

    def discard(self) -> None:
        """Empties the open batch and puts run.downloads back as the last commit left it."""
        self.archive.discard()
        self.run.downloads[:] = self._committed_downloads
        self._settled.clear()
        self._requires.clear()

    def forget_downloads(self, keys: Collection[DownloadKey]) -> None:
        """Takes out of run.downloads the states a commit already deleted, so the run does not
        download them."""
        self.run.downloads[:] = [s for s in self.run.downloads if _download_key(s) not in keys]
        self._committed_downloads = [
            s for s in self._committed_downloads if _download_key(s) not in keys
        ]

    def require(self, ref: DirectoryRef) -> None:
        """The next commit fails unless the directory row exists."""
        self._requires.add(ref)

    def settle(self, key: DownloadKey) -> None:
        """The next commit deletes the key's download state."""
        self._settled.add(key)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._report("saving")
        try:
            # A run that ends normally commits what is left open; a run that raised keeps
            # only what it committed.
            if exc_type is None:
                self.commit()
        except CommitConflict as error:
            self.run.errors.append({"error": str(error)})
        finally:
            self.discard()
            self.run.finished = fetched_at(self.clock.now())
            try:
                self.archive.append_run(self.run, self.slarc_version)
            except Exception:
                # The run's own error is the one to report.
                if exc is None:
                    raise
        self._report("compacting")
        try:
            self.maintained = self.archive.maintain(
                self.blobs, lambda: self._report("deleting unused files")
            )
        except Exception:
            if exc is None:
                raise
        else:
            self._report("saved", finished=True)

    def _report(self, step: str, *, finished: bool = False) -> None:
        if self.reporter is not None:
            self.reporter.archive(step, finished=finished)

    def _current(self, item: TrackedItem) -> TrackedItem:
        revision = self._revisions.get(item.key)
        if revision is None:
            return item
        return msgspec.structs.replace(item, revision=revision)


def _download_key(state: DownloadState) -> DownloadKey:
    return (state.team_id, state.target)
