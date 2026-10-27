# ABOUTME: slarc listen: a catch-up pull, then hour-long segments applying batches of the changes
# ABOUTME: RTM events ask of tracked items, with re-reads of quiet messages and pulls after a loss.
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from slarc.domain.directory import DirectoryView
from slarc.domain.events import (
    Change,
    DeleteMessage,
    EmojiChanged,
    Reread,
    StoreMessage,
    UserChanged,
    classify,
)
from slarc.domain.links import (
    ConversationSel,
    Selector,
    WorkspaceSel,
    message_link,
    parse_selector,
)
from slarc.domain.tracking import TrackedItem, covers
from slarc.ports import (
    AuthFailed,
    Clock,
    CommitConflict,
    Downloader,
    EventConnector,
    NotVisible,
    Reporter,
    SlackConnector,
    SlarcError,
    ThreadNotFound,
)
from slarc.service.appends import append_directory
from slarc.service.downloads import drain_downloads
from slarc.service.held_conversation import HeldConversation
from slarc.service.ingest import MessageWriter
from slarc.service.listen_connections import Connections
from slarc.service.pull import PullOptions, PullReport, pull
from slarc.service.workspaces import Workspaces
from slarc.unit_of_work import UnitOfWork

RTM_API = "rtm"
EMOJI_API = "emoji.list"
QUIET_S = 10.0
FLUSH_QUIET_S = 2.0
FLUSH_MAX_S = 10.0
SEGMENT = timedelta(hours=1)
POLL_S = 1.0

type PendingKey = tuple[str, str, str]  # team, channel, thread parent or lone message ts


@dataclass(frozen=True)
class ListenOptions:
    downloads: int
    max_file_bytes: int
    active_thread_days: int = 7
    unheard_pull_minutes: int = 15


@dataclass
class Pending:
    """Messages waiting to be re-read until they have been quiet for QUIET_S, and the workspaces
    whose next step is a catch-up pull."""

    catch_up: set[str] = field(default_factory=set)
    _marked: dict[PendingKey, datetime] = field(default_factory=dict)

    def mark(self, team_id: str, channel: str, ts: str, at: datetime) -> None:
        self._marked[(team_id, channel, ts)] = at

    def waiting(self) -> bool:
        return bool(self._marked)

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


def listen(
    selected: list[str],
    options: ListenOptions,
    open_uow: Callable[[str], UnitOfWork],
    connector: SlackConnector,
    events: EventConnector,
    downloader: Downloader,
    reporter: Reporter,
    clock: Clock,
    stop: Callable[[], bool],
) -> None:
    """Pulls the selected workspaces, or every workspace with a tracked item, then applies
    their events until stop returns true. A workspace whose connection was lost, or whose batch
    conflicted with another command, is pulled again before listening goes on.

    open_uow gives a fresh unit of work over the open archive for each pull and segment.
    AuthFailed propagates after the open segment ends.
    """
    listener = _Listener(options, open_uow, connector, downloader, reporter, clock)
    teams = listener.catch_up([WorkspaceSel(name) for name in selected], announce=True)
    connections = Connections(events, clock, teams)
    pending = Pending()
    try:
        while not stop():
            listener.segment(connections, pending, clock, stop)
            due, pending.catch_up = sorted(pending.catch_up), set()
            if due:
                listener.catch_up([WorkspaceSel(team) for team in due])
            if listener.unheard_due():
                listener.pull_unheard()
    finally:
        connections.close()


class _Listener:
    def __init__(
        self,
        options: ListenOptions,
        open_uow: Callable[[str], UnitOfWork],
        connector: SlackConnector,
        downloader: Downloader,
        reporter: Reporter,
        clock: Clock,
    ) -> None:
        self.options = options
        self.open_uow = open_uow
        self.connector = connector
        self.downloader = downloader
        self.reporter = reporter
        self.clock = clock
        self.domains: dict[str, str] = {}
        self.unheard: list[TrackedItem] = []
        self.unheard_at = clock.now()
        self._tracked: list[TrackedItem] = []

    def catch_up(self, selectors: list[Selector], *, announce: bool = False) -> list[str]:
        """Pulls the selectors' workspaces, or everything tracked, and returns their team ids.
        Also finds again the tracked items listen cannot hear."""
        report, domains, unheard = self._pull(selectors)
        self.domains |= domains
        self.unheard = [item for item in unheard if item.team_id in self.domains]
        teams = sorted(domains)
        for team_id in teams:
            name = domains[team_id] or team_id
            if not announce:
                self.reporter.info(
                    f"{name}  pulled to catch up: {report.messages_new:,} new messages"
                )
                continue
            count = sum(1 for item in self._tracked if item.team_id == team_id)
            noun = "tracked item" if count == 1 else "tracked items"
            not_heard = ""
            missed = sum(1 for item in self.unheard if item.team_id == team_id)
            if missed:
                minutes = self.options.unheard_pull_minutes
                not_heard = f"; {missed:,} not heard, pulled every {minutes} minutes"
            self.reporter.info(f"Listening to {name} ({count:,} {noun}{not_heard}).")
        return teams

    def unheard_due(self) -> bool:
        elapsed = (self.clock.now() - self.unheard_at).total_seconds()
        return bool(self.unheard) and elapsed >= self.options.unheard_pull_minutes * 60

    def pull_unheard(self) -> None:
        """Pulls the tracked items listen cannot hear."""
        self.unheard_at = self.clock.now()
        count = len(self.unheard)
        selectors = [_selector(self.domains[item.team_id], item) for item in self.unheard]
        report, _, self.unheard = self._pull(selectors)
        noun = "item" if count == 1 else "items"
        self.reporter.info(
            f"pulled {count:,} {noun} not heard: {report.messages_new:,} new messages"
        )

    def _pull(
        self, selectors: list[Selector]
    ) -> tuple[PullReport, dict[str, str], list[TrackedItem]]:
        """Pulls in its own unit of work; returns the report, the domain of each workspace it
        connected to, and the tracked items listen cannot hear."""
        pull_options = PullOptions(
            downloads=self.options.downloads,
            max_file_bytes=self.options.max_file_bytes,
            active_thread_days=self.options.active_thread_days,
        )
        with self.open_uow("listen") as uow:
            report = pull(
                selectors, pull_options, uow, self.connector, self.downloader, self.reporter
            )
            domains = {entry["team_id"]: entry["domain"] for entry in uow.run.auth}
            self._tracked = list(uow.archive.tracked_items().values())
            conversations: dict[str, dict] = {}
            for team_id in domains:
                conversations |= uow.archive.directory_objects(team_id, "conversation")
        return report, domains, unheard_items(self._tracked, conversations)

    def segment(
        self, connections: Connections, pending: Pending, clock: Clock, stop: Callable[[], bool]
    ) -> None:
        """Applies events in batches for up to SEGMENT, ending early when stop returns true or a
        workspace needs a catch-up pull, then closes the unit of work."""
        with self.open_uow("listen") as uow:
            workspaces = Workspaces(self.connector, uow)
            started = clock.now()
            buffered: dict[str, list[Change]] = {}
            first = last = flushed = started

            def flush() -> None:
                nonlocal flushed
                teams = set(buffered) | (set(connections.teams) if pending.waiting() else set())
                for team_id in sorted(teams):
                    said = apply_batch(
                        uow,
                        workspaces,
                        team_id,
                        buffered.get(team_id, []),
                        pending,
                        self.downloader,
                        self.options,
                        self.reporter,
                    )
                    for line in said:
                        self.reporter.info(line)
                buffered.clear()
                flushed = clock.now()

            while True:
                if stop():
                    flush()
                    return
                for team_id, event in connections.poll(POLL_S):
                    change = classify(event)
                    if change is None:
                        continue
                    now = clock.now()
                    if not buffered:
                        first = now
                    last = now
                    buffered.setdefault(team_id, []).append(change)
                pending.catch_up |= connections.take_lost()
                now = clock.now()
                quiet = (now - last).total_seconds() >= FLUSH_QUIET_S
                aged = (now - first).total_seconds() >= FLUSH_MAX_S
                rereads = pending.waiting() and (now - flushed).total_seconds() >= FLUSH_QUIET_S
                if (buffered and (quiet or aged)) or rereads:
                    flush()
                if pending.catch_up or now - started >= SEGMENT or self.unheard_due():
                    flush()
                    return


def unheard_items(items: list[TrackedItem], conversations: dict[str, dict]) -> list[TrackedItem]:
    """The items in a channel whose stored conversations.info says the user is not a member;
    RTM sends no events for them. A DM, a group DM or an unknown channel counts as heard."""
    return [
        item for item in items if (conversations.get(item.channel) or {}).get("is_member") is False
    ]


def _selector(domain: str, item: TrackedItem) -> Selector:
    if item.kind == "conversation" or item.ts is None:
        return ConversationSel(domain=domain, channel=item.channel, name=None, dm_user=None)
    return parse_selector(message_link(domain, item.channel, item.ts))


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
