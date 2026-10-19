# ABOUTME: Unit of work scoping one run over the archive and blob stores.
# ABOUTME: Starts the run record on enter and appends the run line on exit, however the run ends.
from types import TracebackType

from slarc.domain.envelope import fetched_at, run_id
from slarc.domain.tracking import RunRecord
from slarc.ports import ArchiveStore, BlobStore, Clock


class UnitOfWork:
    run: RunRecord

    def __init__(
        self,
        archive: ArchiveStore,
        blobs: BlobStore,
        clock: Clock,
        command: str,
        args: list[str],
        slarc_version: str,
    ) -> None:
        self.archive = archive
        self.blobs = blobs
        self.clock = clock
        self.command = command
        self.args = args
        self.slarc_version = slarc_version

    def __enter__(self) -> UnitOfWork:
        now = self.clock.now()
        self.run = RunRecord(
            run=run_id(now), command=self.command, args=list(self.args), started=fetched_at(now)
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.run.finished = fetched_at(self.clock.now())
        self.archive.append_run(self.run, self.slarc_version)
