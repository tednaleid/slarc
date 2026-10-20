# ABOUTME: What the archive holds for one conversation, kept current as a pull appends lines.
# ABOUTME: Indexes stored messages and threads and tracks what a run has listed, read or failed.
from functools import cached_property

from slarc.domain.links import MessageTs
from slarc.domain.tracking import DownloadState, TrackedItem
from slarc.ports import ArchiveStore, DirectoryState, StoredMessage


class HeldConversation:
    """What the archive holds for one conversation, read once and kept current as lines append.

    listed holds the ts of each message history returned in this run, read_threads the
    thread_ts of each thread read in this run, and failed_threads those whose read failed.
    """

    def __init__(self, archive: ArchiveStore, team_id: str, channel: str) -> None:
        self.team_id = team_id
        self.channel = channel
        self._archive = archive
        self.stored = archive.stored_messages(team_id, channel)
        self.listed: set[str] = set()
        self.read_threads: set[str] = set()
        self.failed_threads: set[str] = set()
        self._replies: dict[str, set[str]] = {}
        for ts, stored in self.stored.items():
            self._index(ts, stored)

    @cached_property
    def files(self) -> dict[str, DirectoryState]:
        return self._archive.directory_state(self.team_id, "file")

    @cached_property
    def images(self) -> dict[str, DirectoryState]:
        return self._archive.directory_state(self.team_id, "image")

    @cached_property
    def downloads(self) -> dict[tuple[str, str], DownloadState]:
        return self._archive.downloads()

    def record(self, ts: str, stored: StoredMessage) -> None:
        self.stored[ts] = stored
        self._index(ts, stored)

    def thread(self, thread_ts: str) -> list[str]:
        """The stored ts of a thread's parent and replies, oldest first."""
        members = set(self._replies.get(thread_ts, ()))
        if thread_ts in self.stored:
            members.add(thread_ts)
        return sorted(members, key=MessageTs)

    def retry_threads(self, item: TrackedItem) -> list[str]:
        """The threads the next pull re-reads first, oldest first.

        They are the threads whose read failed in this run and the item's earlier ones not read
        since, so a pull interrupted after a failure still keeps them.
        """
        retry = self.failed_threads | (set(item.retry_threads) - self.read_threads)
        return sorted(retry, key=MessageTs)

    def has_replies(self, thread_ts: str) -> bool:
        return any(ts != thread_ts for ts in self._replies.get(thread_ts, ()))

    def _index(self, ts: str, stored: StoredMessage) -> None:
        if stored.thread_ts is not None:
            self._replies.setdefault(stored.thread_ts, set()).add(ts)
