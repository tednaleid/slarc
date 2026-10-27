# ABOUTME: slarc listen's service: applies batches of changes RTM events ask for to tracked items,
# ABOUTME: re-reading a message once it has been quiet, and returns a line per change made.
from dataclasses import dataclass, field
from datetime import datetime

from slarc.domain.directory import DirectoryView
from slarc.domain.events import (
    Change,
    DeleteMessage,
    EmojiChanged,
    Reread,
    StoreMessage,
    UserChanged,
)
from slarc.domain.tracking import TrackedItem, covers
from slarc.ports import (
    AuthFailed,
    CommitConflict,
    Downloader,
    NotVisible,
    Reporter,
    SlarcError,
    ThreadNotFound,
)
from slarc.service.appends import append_directory
from slarc.service.downloads import drain_downloads
from slarc.service.held_conversation import HeldConversation
from slarc.service.ingest import MessageWriter
from slarc.service.workspaces import Workspaces
from slarc.unit_of_work import UnitOfWork

RTM_API = "rtm"
EMOJI_API = "emoji.list"
QUIET_S = 10.0

type PendingKey = tuple[str, str, str]  # team, channel, thread parent or lone message ts


@dataclass(frozen=True)
class ListenOptions:
    downloads: int
    max_file_bytes: int
    unheard_pull_minutes: int = 15


@dataclass
class Pending:
    """Messages waiting to be re-read until they have been quiet for QUIET_S, and the workspaces
    whose next step is a catch-up pull."""

    catch_up: set[str] = field(default_factory=set)
    _marked: dict[PendingKey, datetime] = field(default_factory=dict)

    def mark(self, team_id: str, channel: str, ts: str, at: datetime) -> None:
        self._marked[(team_id, channel, ts)] = at

    def due(self, now: datetime, team_id: str | None = None) -> list[PendingKey]:
        """The marked messages quiet for QUIET_S, in team_id when given; each is taken once."""
        ready = sorted(
            key
            for key, at in self._marked.items()
            if (team_id is None or key[0] == team_id) and (now - at).total_seconds() >= QUIET_S
        )
        for key in ready:
            del self._marked[key]
        return ready


def apply_batch(
    uow: UnitOfWork,
    workspaces: Workspaces,
    team_id: str,
    changes: list[Change],
    pending: Pending,
    downloader: Downloader,
    options: ListenOptions,
    reporter: Reporter,
) -> list[str]:
    """Writes the changes that tracked items cover, re-reads the workspace's quiet messages,
    commits, then downloads what this batch queued. Returns one line per change made.

    A commit conflict, such as an rm of an item meanwhile, discards the batch and adds the
    workspace to pending.catch_up.
    """
    batch = _Batch(uow, workspaces, team_id, options)
    for change in changes:
        batch.apply(change, pending)
    batch.reread(pending.due(uow.clock.now(), team_id))
    try:
        uow.commit(unchanged=list(batch.covering.values()))
    except CommitConflict:
        pending.catch_up.add(team_id)
        labels = dict.fromkeys(batch.label(item.channel) for item in batch.covering.values())
        return [f"{label}  changed meanwhile; pulled next" for label in labels]
    drain_downloads(
        uow,
        [team_id],
        downloader,
        options.downloads,
        options.max_file_bytes,
        workspaces,
        reporter,
        earlier=False,
    )
    return batch.said


class _Batch:
    def __init__(
        self, uow: UnitOfWork, workspaces: Workspaces, team_id: str, options: ListenOptions
    ) -> None:
        self.uow = uow
        self.team_id = team_id
        self.connection = workspaces.connection_to(team_id)
        archive = uow.archive
        self.items = [i for i in archive.tracked_items().values() if i.team_id == team_id]
        self.view = _stored_view(uow, team_id)
        self.appended = 0
        self.writer = MessageWriter(uow, options.max_file_bytes, on_new=self._count_new)
        self.held: dict[str, HeldConversation] = {}
        self.covering: dict[tuple[str, str, str, str], TrackedItem] = {}
        self.said: list[str] = []

    def apply(self, change: Change, pending: Pending) -> None:
        match change:
            case StoreMessage(channel, message):
                self._store(channel, message)
            case DeleteMessage(channel, ts):
                self._delete(channel, ts)
            case Reread(channel, ts):
                held = self._conversation(channel)
                stored = held.stored.get(ts)
                thread_ts = stored.thread_ts if stored is not None else None
                if self._cover(channel, ts, thread_ts, keep=False) is not None:
                    pending.mark(self.team_id, channel, thread_ts or ts, self.uow.clock.now())
            case UserChanged(user):
                self._user(user)
            case EmojiChanged():
                self._emoji()

    def reread(self, due: list[tuple[str, str, str]]) -> None:
        gateway = self.connection.gateway
        for _, channel, ts in due:
            if self._cover(channel, ts, None) is None:
                continue
            try:
                messages = gateway.replies(channel, ts)
            except ThreadNotFound, NotVisible:
                continue
            except AuthFailed:
                raise
            except SlarcError as error:
                self.said.append(f"{self.label(channel)}  could not re-read thread {ts}: {error}")
                continue
            self.writer.append_unknown(self.connection, messages, self.view)
            self.writer.append_thread(self._conversation(channel), ts, messages, self.view)
            self.said.append(f"{self.label(channel)}  re-read thread {ts}")

    def label(self, channel: str) -> str:
        return self.view.conversation_label(channel)

    def _store(self, channel: str, message: dict) -> None:
        ts = message["ts"]
        if self._cover(channel, ts, message.get("thread_ts")) is None:
            return
        held = self._conversation(channel)
        stored = held.stored.get(ts)
        before = self.appended
        self.writer.append_unknown(self.connection, [message], self.view)
        self.writer.append_messages(held, [message], RTM_API, self.view)
        if self.appended > before:
            verb = "edited" if stored is not None and stored.hash is not None else "new"
            author = self.view.author_real_name(message)
            self.said.append(f"{self.label(channel)}  {verb} message from {author}")

    def _delete(self, channel: str, ts: str) -> None:
        held = self._conversation(channel)
        stored = held.stored.get(ts)
        if stored is None or stored.hash is None:
            return
        if self._cover(channel, ts, stored.thread_ts) is None:
            return
        self.writer.append_deletes(held, {ts})
        self.said.append(f"{self.label(channel)}  deleted message {ts}")

    def _user(self, user: dict) -> None:
        user_id = user.get("id")
        if not user_id:
            return
        appended = append_directory(self.uow, self.team_id, "user", {user_id: user}, RTM_API)
        if appended:
            self.view.users[user_id] = user
            self.said.append(
                f"{self.view.author_real_name({'user': user_id})} changed their profile"
            )

    def _emoji(self) -> None:
        listed = self.connection.gateway.emoji()
        emoji = {name: {"name": name, "url": url} for name, url in listed.items()}
        if append_directory(self.uow, self.team_id, "emoji", emoji, EMOJI_API):
            self.view.emoji.update(listed)
            self.said.append("custom emoji changed")

    def _cover(
        self, channel: str, ts: str, thread_ts: str | None, *, keep: bool = True
    ) -> TrackedItem | None:
        item = next((i for i in self.items if covers(i, channel, ts, thread_ts)), None)
        if item is not None and keep:
            self.covering[item.key] = item
        return item

    def _conversation(self, channel: str) -> HeldConversation:
        if channel not in self.held:
            self.held[channel] = HeldConversation(self.uow.archive, self.team_id, channel)
        return self.held[channel]

    def _count_new(self) -> None:
        self.appended += 1


def _stored_view(uow: UnitOfWork, team_id: str) -> DirectoryView:
    archive = uow.archive
    emoji = archive.directory_objects(team_id, "emoji")
    return DirectoryView(
        users=archive.directory_objects(team_id, "user"),
        conversations=archive.directory_objects(team_id, "conversation"),
        usergroups=archive.directory_objects(team_id, "usergroup"),
        emoji={name: obj.get("url", "") for name, obj in emoji.items()},
        bots=archive.directory_objects(team_id, "bot"),
    )
