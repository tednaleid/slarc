# ABOUTME: slarc listen: a catch-up pull, then hour-long segments applying batches of the changes
# ABOUTME: RTM events ask of tracked items, with re-reads of quiet messages and pulls after a loss.
from collections.abc import Callable, Mapping
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
from slarc.domain.tracking import ItemKey, TrackedItem, covers
from slarc.ports import (
    ArchiveFailed,
    AuthFailed,
    Clock,
    CommitConflict,
    Downloader,
    EventConnector,
    ListenReporter,
    NoSession,
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
from slarc.service.workspaces import Connection, Workspaces
from slarc.unit_of_work import UnitOfWork

RTM_API = "rtm"
EMOJI_API = "emoji.list"
QUIET_S = 10.0
FLUSH_QUIET_S = 2.0
FLUSH_MAX_S = 10.0
SEGMENT = timedelta(hours=1)
POLL_S = 1.0
SNAPSHOT_S = 5.0
OPEN_POLL_S = 0.1
CATCH_UP_RETRY = timedelta(minutes=1)

type PendingKey = tuple[str, str, str]  # team, channel, thread parent or lone message ts
type Choose = Callable[[Mapping[ItemKey, TrackedItem]], list[Selector]]


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

    def due_teams(self, now: datetime) -> set[str]:
        """The workspaces with a marked message quiet for QUIET_S."""
        return {key[0] for key, at in self._marked.items() if _quiet(now, at)}

    def due(self, now: datetime, team_id: str | None = None) -> list[PendingKey]:
        """The marked messages quiet for QUIET_S, in team_id when given; each is taken once."""
        ready = sorted(
            key
            for key, at in self._marked.items()
            if (team_id is None or key[0] == team_id) and _quiet(now, at)
        )
        for key in ready:
            del self._marked[key]
        return ready


def _quiet(now: datetime, at: datetime) -> bool:
    return (now - at).total_seconds() >= QUIET_S


class TeamState:
    """What a segment keeps for one workspace between batches: its directory, each
    conversation's stored messages, and the writer, which remembers users Slack did not return."""

    def __init__(self, uow: UnitOfWork, team_id: str, options: ListenOptions) -> None:
        self.view = _stored_view(uow, team_id)
        self.held: dict[str, HeldConversation] = {}
        self.appended = 0
        self.writer = MessageWriter(uow, options.max_file_bytes, on_new=self._count_new)

    def _count_new(self) -> None:
        self.appended += 1


def listen(
    selected: list[str],
    options: ListenOptions,
    open_uow: Callable[[str], UnitOfWork],
    connector: SlackConnector,
    events: EventConnector,
    downloader: Downloader,
    reporter: ListenReporter,
    clock: Clock,
    stop: Callable[[], bool],
) -> None:
    """Connects to the selected workspaces, or every workspace with a tracked item, pulls them,
    then applies their events until stop returns true. Connecting first means nothing posted
    during that pull is missed. A workspace whose connection was lost, or whose batch conflicted
    with another command, is pulled again; one Slack cannot be reached for is tried again a
    minute later.

    open_uow gives a fresh unit of work over the open archive for each pull and segment.
    AuthFailed and NoSession propagate, including a workspace's at the first pull.
    """
    listener = _Listener(options, open_uow, connector, downloader, reporter, clock)
    opened: list[Connections] = []

    def connect(teams: list[str]) -> list[tuple[str, dict]]:
        connections = Connections(events, clock, teams)
        opened.append(connections)
        return connections.poll(OPEN_POLL_S)

    try:
        early = listener.start(selected, connect)
        if not opened:
            return
        connections, pending = opened[0], Pending()
        while True:
            # A segment runs even after an early stop, to store what connecting read.
            listener.segment(connections, pending, stop, early)
            early = []
            if stop():
                break
            listener.catch_up(connections, pending)
            if listener.unheard_due():
                listener.pull_unheard()
    finally:
        for connections in opened:
            connections.close()


class _Listener:
    def __init__(
        self,
        options: ListenOptions,
        open_uow: Callable[[str], UnitOfWork],
        connector: SlackConnector,
        downloader: Downloader,
        reporter: ListenReporter,
        clock: Clock,
    ) -> None:
        self.options = options
        self.open_uow = open_uow
        self.connector = connector
        self.downloader = downloader
        self.reporter = reporter
        self.clock = clock
        self.domains: dict[str, str] = {}
        self.every_workspace = True
        self.tracked: dict[ItemKey, TrackedItem] = {}
        self.snapshot: int | None = None
        self.unheard: list[TrackedItem] = []
        self.unheard_at = clock.now()
        self.retry_at = clock.now()

    def start(
        self, selected: list[str], connect: Callable[[list[str]], list[tuple[str, dict]]]
    ) -> list[tuple[str, dict]]:
        """Connects, then pulls, then names each workspace listened to. Returns the events read
        while connecting."""
        early: list[tuple[str, dict]] = []

        def choose(tracked: Mapping[ItemKey, TrackedItem]) -> list[Selector]:
            if selected:
                teams = [self.connector.connect(name).auth_test()["team_id"] for name in selected]
            else:
                teams = sorted({item.team_id for item in tracked.values()})
            self.every_workspace = not selected
            self.tracked = {k: item for k, item in tracked.items() if item.team_id in teams}
            count = len(self.tracked)
            self.reporter.info(f"Pulling {_count(count, 'tracked item')} before listening.")
            early.extend(connect(teams))
            return [WorkspaceSel(team) for team in teams]

        pulled = self._pull(choose, shown=True)
        if pulled is None:
            return early
        report, teams, tracked = pulled
        self.reporter.info(
            f"Pulled {_count(report.items_pulled, 'tracked item')}: "
            f"{_count(report.messages_new, 'new message')}."
        )
        for team_id in sorted(teams):
            name = self.domains.get(team_id) or team_id
            count = sum(1 for item in tracked if item.team_id == team_id)
            not_heard = ""
            missed = sum(1 for item in self.unheard if item.team_id == team_id)
            if missed:
                minutes = self.options.unheard_pull_minutes
                not_heard = f"; {missed:,} not heard, pulled every {minutes} minutes"
            self.reporter.info(f"Listening to {name} ({_count(count, 'tracked item')}{not_heard}).")
        return early

    def notice(
        self,
        uow: UnitOfWork,
        connections: Connections,
        pending: Pending,
        states: dict[str, TeamState],
    ) -> None:
        """Once another commit has changed the archive, finds the tracked items added or removed
        since listen last looked. It names each one, connects to a workspace newly tracked, which
        is pulled once its connection opens, and stops listening to one with nothing tracked."""
        snapshot = uow.archive.snapshot()
        if snapshot == self.snapshot:
            return
        self.snapshot = snapshot
        tracked = {
            key: item
            for key, item in uow.archive.tracked_items().items()
            if self.every_workspace or item.team_id in connections.teams
        }
        added = [tracked[key] for key in sorted(tracked.keys() - self.tracked.keys())]
        removed = [self.tracked[key] for key in sorted(self.tracked.keys() - tracked.keys())]
        if not added and not removed:
            return
        self.tracked = tracked
        self.unheard = self._unheard(uow, list(tracked.values()))
        live = {item.team_id for item in tracked.values()}
        changed = {item.team_id for item in added + removed}
        views = {team_id: _stored_view(uow, team_id) for team_id in changed}
        minutes = self.options.unheard_pull_minutes
        for item in added:
            label = _item_label(views[item.team_id], item)
            if item in self.unheard:
                self.reporter.info(
                    f"Now also pulling {label} every {minutes} minutes; listen cannot hear it."
                )
            else:
                self.reporter.info(f"Now also listening to {label}.")
            if item.team_id not in connections.teams:
                connections.add(item.team_id)
        for item in removed:
            if item.team_id in live:
                self.reporter.info(
                    f"No longer listening to {_item_label(views[item.team_id], item)}."
                )
        for team_id in sorted(changed):
            states.pop(team_id, None)
            if team_id not in live and team_id in connections.teams:
                connections.forget(team_id)
                pending.catch_up.discard(team_id)
                name = self.domains.get(team_id) or team_id
                self.reporter.info(f"{name}  nothing tracked any more; stopped listening")

    def catch_up_ready(self, pending: Pending) -> bool:
        return bool(pending.catch_up) and self.clock.now() >= self.retry_at

    def catch_up(self, connections: Connections, pending: Pending) -> None:
        """Pulls the workspaces pending.catch_up names, and stops listening to any with nothing
        tracked any more. When Slack cannot be reached, they wait CATCH_UP_RETRY."""
        if not self.catch_up_ready(pending):
            return
        due, pending.catch_up = sorted(pending.catch_up), set()

        def choose(tracked: Mapping[ItemKey, TrackedItem]) -> list[Selector]:
            live = {item.team_id for item in tracked.values()}
            for team_id in due:
                if team_id not in live:
                    connections.forget(team_id)
                    name = self.domains.get(team_id) or team_id
                    self.reporter.info(f"{name}  nothing tracked any more; stopped listening")
            return [WorkspaceSel(team) for team in due if team in live]

        try:
            pulled = self._pull(choose, shown=True)
        except AuthFailed, NoSession, ArchiveFailed:
            raise
        except SlarcError as error:
            pending.catch_up |= set(due)
            self.retry_at = self.clock.now() + CATCH_UP_RETRY
            self.reporter.warn(f"{error} Pulling again in a minute.")
            return
        if pulled is not None and pulled[0].messages_new:
            report, teams, _ = pulled
            for team_id in sorted(teams):
                name = self.domains.get(team_id) or team_id
                news = report.messages_new
                self.reporter.info(f"{name}  pulled to catch up: {news:,} new messages")

    def unheard_due(self) -> bool:
        elapsed = (self.clock.now() - self.unheard_at).total_seconds()
        return bool(self.unheard) and elapsed >= self.options.unheard_pull_minutes * 60

    def pull_unheard(self) -> None:
        """Pulls the tracked items listen cannot hear that are still tracked."""
        self.unheard_at = self.clock.now()

        def choose(tracked: Mapping[ItemKey, TrackedItem]) -> list[Selector]:
            still = [item for item in self.unheard if item.key in tracked]
            return [_selector(self.domains[item.team_id], item) for item in still]

        count = len(self.unheard)
        try:
            pulled = self._pull(choose)
        except AuthFailed, NoSession, ArchiveFailed:
            raise
        except SlarcError as error:
            self.reporter.warn(f"{error} Pulling the items not heard again later.")
            return
        if pulled is not None and pulled[0].messages_new:
            news = _count(pulled[0].messages_new, "new message")
            self.reporter.info(f"pulled {_count(count, 'item')} not heard: {news}")

    def segment(
        self,
        connections: Connections,
        pending: Pending,
        stop: Callable[[], bool],
        early: list[tuple[str, dict]],
    ) -> None:
        """Applies events in batches for up to SEGMENT, ending early when stop returns true or a
        catch-up or unheard pull is due, then closes the unit of work."""
        clock = self.clock
        with self.open_uow("listen") as uow:
            workspaces = Workspaces(self.connector, uow)
            states: dict[str, TeamState] = {}
            started = checked = clock.now()
            buffered: dict[str, list[Change]] = {}
            first = last = started

            def take(polled: list[tuple[str, dict]]) -> None:
                nonlocal first, last
                for team_id, event in polled:
                    change = classify(event)
                    if change is None:
                        continue
                    now = clock.now()
                    if not buffered:
                        first = now
                    last = now
                    buffered.setdefault(team_id, []).append(change)

            def flush(teams: set[str]) -> None:
                for team_id in sorted(teams):
                    if team_id not in states:
                        states[team_id] = TeamState(uow, team_id, self.options)
                    said = apply_batch(
                        uow,
                        workspaces,
                        team_id,
                        buffered.pop(team_id, []),
                        pending,
                        self.downloader,
                        self.options,
                        self.reporter,
                        states[team_id],
                    )
                    if team_id in pending.catch_up:
                        states.pop(team_id, None)
                    for line in said:
                        self.reporter.info(line)

            take(early)
            while True:
                if stop():
                    flush(set(buffered) | pending.due_teams(clock.now()))
                    return
                take(connections.poll(POLL_S))
                now = clock.now()
                if (now - checked).total_seconds() >= SNAPSHOT_S:
                    checked = now
                    self.notice(uow, connections, pending, states)
                pending.catch_up |= connections.take_lost()
                quiet = (now - last).total_seconds() >= FLUSH_QUIET_S
                aged = (now - first).total_seconds() >= FLUSH_MAX_S
                teams = set(buffered) if buffered and (quiet or aged) else set()
                teams |= pending.due_teams(now)
                if teams:
                    flush(teams)
                ending = self.catch_up_ready(pending) or self.unheard_due()
                if ending or now - started >= SEGMENT:
                    flush(set(buffered) | pending.due_teams(now))
                    return

    def _pull(
        self, choose: Choose, *, shown: bool = False
    ) -> tuple[PullReport, set[str], list[TrackedItem]] | None:
        """Pulls what choose picks from the tracked items, in its own unit of work, and finds
        again the tracked items listen cannot hear. Returns the report, the workspaces pulled
        and the tracked items, or None when choose picked nothing. A shown pull's progress is
        reported between pull_started and pull_ended.

        A workspace whose session is missing or rejected raises its error.
        """
        pull_options = PullOptions(
            downloads=self.options.downloads,
            max_file_bytes=self.options.max_file_bytes,
            active_thread_days=self.options.active_thread_days,
        )
        with self.open_uow("listen") as uow:
            selectors = choose(uow.archive.tracked_items())
            if not selectors:
                return None
            if shown:
                self.reporter.pull_started()
            try:
                report = pull(
                    selectors, pull_options, uow, self.connector, self.downloader, self.reporter
                )
            finally:
                if shown:
                    self.reporter.pull_ended()
            if report.unreachable:
                raise report.unreachable[0]
            domains = {entry["team_id"]: entry["domain"] for entry in uow.run.auth}
            self.domains |= domains
            tracked = list(uow.archive.tracked_items().values())
            self.unheard = self._unheard(uow, tracked)
        return report, set(domains), tracked

    def _unheard(self, uow: UnitOfWork, tracked: list[TrackedItem]) -> list[TrackedItem]:
        """The tracked items listen cannot hear, in the workspaces a pull has named."""
        conversations: dict[str, dict] = {}
        for team_id in self.domains:
            conversations |= uow.archive.directory_objects(team_id, "conversation")
        return [
            item for item in unheard_items(tracked, conversations) if item.team_id in self.domains
        ]


def _item_label(view: DirectoryView, item: TrackedItem) -> str:
    conversation = view.conversation_label(item.channel)
    if item.kind == "conversation":
        return conversation
    return f"{item.kind} {item.ts} in {conversation}"


def _count(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


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
    state: TeamState | None = None,
) -> list[str]:
    """Writes the changes that tracked items cover, re-reads the workspace's quiet messages,
    commits when anything changed, then downloads what this batch queued. Returns one line per
    change made. state carries what an earlier batch of the same segment read.

    A commit conflict, such as an rm of an item meanwhile, or Slack being unreachable, discards
    the batch and adds the workspace to pending.catch_up; the caller then drops state.
    """
    state = state if state is not None else TeamState(uow, team_id, options)
    batch = _Batch(uow, workspaces, team_id, state)
    try:
        for change in changes:
            batch.apply(change, pending)
        batch.reread(pending.due(uow.clock.now(), team_id))
    except AuthFailed, NoSession, ArchiveFailed:
        raise
    except SlarcError as error:
        uow.discard()
        pending.catch_up.add(team_id)
        return [f"{batch.workspace}  {error} Pulling it next."]
    if not batch.changed:
        return batch.said
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
        self, uow: UnitOfWork, workspaces: Workspaces, team_id: str, state: TeamState
    ) -> None:
        self.uow = uow
        self.team_id = team_id
        self.workspaces = workspaces
        archive = uow.archive
        self.items = [i for i in archive.tracked_items().values() if i.team_id == team_id]
        self.state = state
        self.view = state.view
        self.writer = state.writer
        self.appended_before = state.appended
        self.directory_changed = False
        self.covering: dict[ItemKey, TrackedItem] = {}
        self.said: list[str] = []

    @property
    def changed(self) -> bool:
        return self.state.appended > self.appended_before or self.directory_changed

    @property
    def workspace(self) -> str:
        return self.team_id

    def connection(self) -> Connection:
        """The workspace's connection, made on first use, since a batch of untracked or
        unchanged events needs none."""
        return self.workspaces.connection_to(self.team_id)

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
            case UserChanged(user) if self.items:
                self._user(user)
            case EmojiChanged() if self.items:
                self._emoji()

    def reread(self, due: list[tuple[str, str, str]]) -> None:
        for _, channel, ts in due:
            if self._cover(channel, ts, None) is None:
                continue
            connection = self.connection()
            try:
                messages = connection.gateway.replies(channel, ts)
            except ThreadNotFound, NotVisible:
                continue
            except AuthFailed:
                raise
            except SlarcError as error:
                self.said.append(f"{self.label(channel)}  could not re-read thread {ts}: {error}")
                continue
            self.writer.append_unknown(connection, messages, self.view)
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
        before = self.state.appended
        self.writer.append_unknown(self.connection(), [message], self.view)
        self.writer.append_messages(held, [message], RTM_API, self.view)
        if self.state.appended > before:
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
            self.directory_changed = True
            self.view.users[user_id] = user
            self.said.append(
                f"{self.view.author_real_name({'user': user_id})} changed their profile"
            )

    def _emoji(self) -> None:
        listed = self.connection().gateway.emoji()
        emoji = {name: {"name": name, "url": url} for name, url in listed.items()}
        if append_directory(self.uow, self.team_id, "emoji", emoji, EMOJI_API):
            self.directory_changed = True
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
        held = self.state.held
        if channel not in held:
            held[channel] = HeldConversation(self.uow.archive, self.team_id, channel)
        return held[channel]


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
