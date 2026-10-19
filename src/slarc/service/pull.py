# ABOUTME: The pull use case: refreshes the workspace directory and the tracked items it is given.
# ABOUTME: Appends a line only for a new version, records deletes and downloads files and images.
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property

import msgspec

from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.hashing import content_hash
from slarc.domain.links import ConversationSel, MessageSel, MessageTs, Selector, message_link
from slarc.domain.plain import plain_text
from slarc.domain.tracking import DownloadState, TrackedItem
from slarc.ports import (
    ArchiveStore,
    AuthFailed,
    DirectoryState,
    Downloader,
    NotVisible,
    Reporter,
    Session,
    SlackConnector,
    SlackGateway,
    SlarcError,
    StoredMessage,
    ThreadNotFound,
)
from slarc.service.appends import (
    LINES_APPENDED,
    append_directory,
    append_line,
    count,
    fetched_now,
    stamped_meta,
)
from slarc.service.auth import domain_of
from slarc.service.downloads import drain_downloads, queue_downloads
from slarc.service.selectors import only_workspace, resolve_conversation
from slarc.service.threads import active_threads, discover_reply_threads, infer_deletes
from slarc.unit_of_work import UnitOfWork

HISTORY_API = "conversations.history"
REPLIES_API = "conversations.replies"
BOTS_API = "bots.info"
MESSAGES_SEEN = "messages_seen"

type ItemKey = tuple[str, str, str, str]


@dataclass
class PullOptions:
    since: str | None = None
    until: str | None = None
    full: bool = False
    channel_mode: bool = False
    downloads: int = 4
    max_file_bytes: int = 100 * 1024 * 1024
    active_thread_days: int = 7


@dataclass
class PullReport:
    lines_appended: int
    messages_seen: int
    skipped: list[str]
    downloads_pending: int


def pull(
    selectors: list[Selector],
    options: PullOptions,
    uow: UnitOfWork,
    connector: SlackConnector,
    downloader: Downloader,
    reporter: Reporter,
) -> PullReport:
    """Pulls the selected items, or every tracked item when selectors is empty."""
    run = _PullRun(options, uow, reporter, _Workspaces(connector, uow))
    items = run.resolve(selectors)
    for team_id in dict.fromkeys(item.team_id for item in items):
        run.pull_workspace(team_id, [i for i in items if i.team_id == team_id])
    pending = drain_downloads(
        uow, downloader, options.downloads, options.max_file_bytes, run.workspaces
    )
    counts = uow.run.counts
    return PullReport(
        lines_appended=int(counts.get(LINES_APPENDED, 0)),
        messages_seen=int(counts.get(MESSAGES_SEEN, 0)),
        skipped=run.skipped,
        downloads_pending=pending,
    )


def refresh_directory(
    gateway: SlackGateway,
    team_id: str,
    channels: set[str],
    uow: UnitOfWork,
    bot_ids: Iterable[str] = (),
) -> DirectoryView:
    """Appends changed directory lines and returns the directory with the objects just read.

    Bots are fetched only for the given ids that the archive does not hold yet.
    """
    archive = uow.archive
    users = {user["id"]: user for user in gateway.users()}
    emoji = {name: {"name": name, "url": url} for name, url in gateway.emoji().items()}
    usergroups = {group["id"]: group for group in gateway.usergroups()}
    bots = _unknown_bots(gateway, team_id, bot_ids, archive)
    conversations = {}
    for channel in sorted(channels):
        try:
            conversations[channel] = gateway.conversation_info(channel)
        except NotVisible:
            continue

    fresh: list[tuple[Kind, str, dict[str, dict]]] = [
        ("user", "users.list", users),
        ("emoji", "emoji.list", emoji),
        ("usergroup", "usergroups.list", usergroups),
        ("bot", BOTS_API, bots),
        ("conversation", "conversations.info", conversations),
    ]
    for kind, api, objects in fresh:
        append_directory(uow, team_id, kind, objects, api)

    def current(kind: Kind, objects: dict[str, dict]) -> dict[str, dict]:
        return archive.directory_objects(team_id, kind) | objects

    return DirectoryView(
        users=current("user", users),
        conversations=current("conversation", conversations),
        usergroups=current("usergroup", usergroups),
        emoji={name: obj.get("url", "") for name, obj in current("emoji", emoji).items()},
        bots=current("bot", bots),
    )


def date_to_ts(date: str, end_of_day: bool) -> str:
    """The ts at the start of a YYYY-MM-DD day in UTC, or with end_of_day the start of the next.

    History bounds are exclusive, so the next day's start ends the range after the whole day.
    """
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    if end_of_day:
        start += timedelta(days=1)
    return f"{int(start.timestamp())}.000000"


def _unknown_bots(
    gateway: SlackGateway, team_id: str, bot_ids: Iterable[str], archive: ArchiveStore
) -> dict[str, dict]:
    """The bots.info object of each given bot the archive does not hold yet."""
    wanted = set(bot_ids)
    if not wanted:
        return {}
    bots = {}
    for bot_id in sorted(wanted - archive.directory_state(team_id, "bot").keys()):
        try:
            bots[bot_id] = gateway.bot_info(bot_id)
        except AuthFailed:
            raise
        except SlarcError:
            # A bot Slack no longer knows still renders from the message's bot_profile.
            continue
    return bots


@dataclass(frozen=True)
class _Connection:
    team_id: str
    user_id: str
    domain: str
    gateway: SlackGateway


class _Workspaces:
    """Connects once per workspace in a run, since each connection has its own rate limiter.

    Each connection is recorded in the run's teams and auth entries. As a SlackConnector it
    hands the run's connections to the download drain.
    """

    def __init__(self, connector: SlackConnector, uow: UnitOfWork) -> None:
        self._connector = connector
        self._uow = uow
        self._team_of: dict[str, str] = {}
        self._connections: dict[str, _Connection] = {}

    def sessions(self) -> list[Session]:
        return self._connector.sessions()

    def connect(self, workspace: str) -> SlackGateway:
        return self.connection(workspace).gateway

    def connection(self, workspace: str) -> _Connection:
        """Takes a domain or team id."""
        team_id = self._team_of.get(workspace, workspace)
        if team_id not in self._connections:
            connection = self._open(workspace)
            team_id = connection.team_id
            self._connections[team_id] = connection
        self._team_of[workspace] = team_id
        return self._connections[team_id]

    def _open(self, workspace: str) -> _Connection:
        gateway = self._connector.connect(workspace)
        info = gateway.auth_test()
        session = next((s for s in self._connector.sessions() if s.matches(workspace)), None)
        domain = (session.domain if session is not None else "") or domain_of(info)
        run = self._uow.run
        run.teams.append(info["team_id"])
        run.auth.append(
            {
                "team_id": info["team_id"],
                "user_id": info["user_id"],
                "domain": domain,
                "credential_source": session.source if session is not None else "",
                "auth_test_at": fetched_now(self._uow),
            }
        )
        return _Connection(info["team_id"], info["user_id"], domain, gateway)


def _missing_message(domain: str, channel: str, ts: str) -> str:
    return f"Slack returns no message at {message_link(domain, channel, ts)}; skipping it."


class _PullRun:
    def __init__(
        self,
        options: PullOptions,
        uow: UnitOfWork,
        reporter: Reporter,
        workspaces: _Workspaces,
    ) -> None:
        self.options = options
        self.uow = uow
        self.reporter = reporter
        self.workspaces = workspaces
        self.prefetched: dict[ItemKey, list[dict]] = {}
        self.skipped: list[str] = []

    def resolve(self, selectors: list[Selector]) -> list[TrackedItem]:
        """The items to pull; a thread read while working out a selector's kind is kept."""
        tracked = self.uow.archive.tracked_items()
        if not selectors:
            return list(tracked.values())
        items: dict[ItemKey, TrackedItem] = {}
        for selector in selectors:
            connection = self.workspaces.connection(selector.domain or only_workspace(tracked))
            if isinstance(selector, ConversationSel):
                channel = resolve_conversation(
                    selector, connection.team_id, connection.gateway, self.uow.archive
                )
                item = TrackedItem(team_id=connection.team_id, kind="conversation", channel=channel)
            else:
                item = self._message_item(selector, connection, tracked)
            if item is None:
                continue
            known = tracked.get(item.key)
            item = self._ranged(item, known) if item.kind == "conversation" else known or item
            items.setdefault(item.key, item)
        return list(items.values())

    def _ranged(self, item: TrackedItem, known: TrackedItem | None) -> TrackedItem:
        """A new conversation takes the options' date range; a tracked one only widens to it.

        Widening since reopens the backfill, which then pages the gap below oldest_ts.
        """
        since = date_to_ts(self.options.since, end_of_day=False) if self.options.since else None
        until = date_to_ts(self.options.until, end_of_day=True) if self.options.until else None
        if known is None:
            return msgspec.structs.replace(item, since=since, until=until)
        if since is not None and known.since is not None and MessageTs(since) < known.since:
            known = msgspec.structs.replace(known, since=since, backfill_complete=False)
        if until is not None and known.until is not None and MessageTs(until) > known.until:
            known = msgspec.structs.replace(known, until=until)
        return known

    def _message_item(
        self, selector: MessageSel, connection: _Connection, tracked: dict[ItemKey, TrackedItem]
    ) -> TrackedItem | None:
        """The item a message link names, or None when Slack returns no such message."""
        team_id, channel, ts = connection.team_id, selector.channel, str(selector.ts)
        if self.options.channel_mode:
            return TrackedItem(team_id=team_id, kind="conversation", channel=channel)
        if selector.thread_ts is not None:
            thread_ts = str(selector.thread_ts)
            return TrackedItem(team_id=team_id, kind="thread", channel=channel, ts=thread_ts)
        for kind in ("thread", "message"):
            known = tracked.get((team_id, kind, channel, ts))
            if known is not None:
                return known
        try:
            messages = connection.gateway.replies(channel, ts)
        except NotVisible:
            # Pulling the item reports that it cannot be read.
            return TrackedItem(team_id=team_id, kind="message", channel=channel, ts=ts)
        except ThreadNotFound:
            self._warn(_missing_message(connection.domain, channel, ts))
            return None
        kind = "thread" if len(messages) > 1 else "message"
        item = TrackedItem(team_id=team_id, kind=kind, channel=channel, ts=messages[0]["ts"])
        self.prefetched[item.key] = messages
        return item

    def pull_workspace(self, team_id: str, items: list[TrackedItem]) -> None:
        connection = self.workspaces.connection(team_id)
        conversations = [item for item in items if item.kind == "conversation"]
        threads = [item for item in items if item.kind != "conversation"]
        reads: dict[ItemKey, list[dict]] = {}
        for item in threads:
            messages = self._read_thread(connection, item)
            if messages is not None:
                reads[item.key] = messages
        bot_ids = {m["bot_id"] for messages in reads.values() for m in messages if m.get("bot_id")}
        channels = {item.channel for item in threads if item.key in reads}
        channels |= {item.channel for item in conversations}
        view = refresh_directory(connection.gateway, team_id, channels, self.uow, bot_ids)
        for item in threads:
            if item.key not in reads:
                continue
            held = _HeldConversation(self.uow.archive, team_id, item.channel)
            self._append_thread(held, item.ts or "", reads[item.key], view)
            self._finish(connection, item, view)
        for item in conversations:
            self._pull_conversation(connection, item, view)

    def _finish(self, connection: _Connection, item: TrackedItem, view: DirectoryView) -> None:
        last_read = dict(item.last_read)
        conversation = view.conversations.get(item.channel, {})
        if conversation.get("last_read"):
            last_read[connection.user_id] = conversation["last_read"]
        self._record(
            msgspec.structs.replace(
                item, status="ok", last_pull_at=fetched_now(self.uow), last_read=last_read
            )
        )

    def _read_thread(self, connection: _Connection, item: TrackedItem) -> list[dict] | None:
        """The thread's messages, parent first, or None when the item cannot be read.

        A thread Slack no longer returns is skipped rather than recorded as deleted, since a
        session that cannot see it gets the same answer.
        """
        if item.key in self.prefetched:
            return self.prefetched[item.key]
        ts = item.ts or ""
        try:
            return connection.gateway.replies(item.channel, ts)
        except NotVisible as error:
            self._skip(item, str(error))
        except ThreadNotFound:
            self._skip(item, _missing_message(connection.domain, item.channel, ts))
        return None

    def _pull_conversation(
        self, connection: _Connection, item: TrackedItem, view: DirectoryView
    ) -> None:
        """Pulls new history and new replies under older parents, then re-reads active threads.

        --full re-reads the whole range instead. A first pull's history covers every reply, so
        it skips search and later pulls search from its start. Any read failing with NotVisible
        skips the item without deletes.
        """
        held = _HeldConversation(self.uow.archive, item.team_id, item.channel)
        started_at_top = not item.backfill_complete and item.oldest_ts is None
        if started_at_top:
            item = msgspec.structs.replace(item, last_search_at=fetched_now(self.uow))
        try:
            if self.options.full and not started_at_top:
                item = self._reread_range(connection, held, item, view)
            else:
                item = self._pull_history(connection, held, item, view, started_at_top)
                if not started_at_top:
                    item = self._search_replies(connection, held, item, view)
            now, days = self.uow.clock.now(), self.options.active_thread_days
            active = active_threads(held.stored, now, days)
            self._reread_threads(connection, held, _within(item, active), view)
        except NotVisible as error:
            self._skip(item, str(error))
            return
        self._finish(connection, item, view)

    def _pull_history(
        self,
        connection: _Connection,
        held: _HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
        started_at_top: bool,
    ) -> TrackedItem:
        """Pages older history while the backfill is incomplete, then newer than newest_ts."""
        if not item.backfill_complete:
            item = self._page_older(connection, held, item, view, item.oldest_ts or item.until)
        if not started_at_top:
            item = self._page_newer(connection, held, item, view, item.newest_ts or item.since)
        return item

    def _page_older(
        self,
        connection: _Connection,
        held: _HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
        latest: str | None,
    ) -> TrackedItem:
        """Pages history from since up to latest and marks the backfill complete.

        The item is recorded after every page, so an interrupted pull resumes below oldest_ts.
        """
        for page in connection.gateway.history(item.channel, item.since, latest):
            self._append_history_page(connection, held, page, view)
            item = msgspec.structs.replace(
                item,
                newest_ts=_outermost(item.newest_ts, page, newest=True),
                oldest_ts=_outermost(item.oldest_ts, page, newest=False),
            )
            self._record(item)
        item = msgspec.structs.replace(item, backfill_complete=True)
        self._record(item)
        return item

    def _page_newer(
        self,
        connection: _Connection,
        held: _HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
        oldest: str | None,
    ) -> TrackedItem:
        """Pages history from oldest up to until.

        Pages come newest first, so newest_ts moves only once paging ends; an interrupted pull
        then pages the same range again.
        """
        newest = item.newest_ts
        for page in connection.gateway.history(item.channel, oldest, item.until):
            self._append_history_page(connection, held, page, view)
            newest = _outermost(newest, page, newest=True)
            oldest_ts = _outermost(item.oldest_ts, page, newest=False)
            item = msgspec.structs.replace(item, oldest_ts=oldest_ts)
            self._record(item)
        return msgspec.structs.replace(item, newest_ts=newest)

    def _search_replies(
        self,
        connection: _Connection,
        held: _HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
    ) -> TrackedItem:
        """Re-reads each thread search finds a message in since two days before the last search.

        last_search_at moves only when the search succeeds, so a failed one is retried.
        """
        searched_at = fetched_now(self.uow)
        if item.last_search_at is not None:
            since_date = _two_days_before(item.last_search_at)
            try:
                threads = discover_reply_threads(connection.gateway, item.channel, since_date)
            except AuthFailed:
                raise
            except SlarcError as error:
                self.reporter.warn(
                    f"Could not search conversation {item.channel} for new replies, so only its "
                    f"active threads were re-read: {error}"
                )
                return item
            self._reread_threads(connection, held, _within(item, threads), view)
        return msgspec.structs.replace(item, last_search_at=searched_at)

    def _reread_range(
        self,
        connection: _Connection,
        held: _HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
    ) -> TrackedItem:
        """Re-reads the whole range and every thread in it, then records deleted messages.

        Each thread re-read records its own deleted replies, so the range covers top-level
        messages only.
        """
        searched_at = fetched_now(self.uow)
        item = self._page_newer(connection, held, item, view, item.since)
        parents = {ts for ts in held.listed if held.has_replies(ts)}
        self._reread_threads(connection, held, parents, view)
        stored = held.stored
        top_level = {
            ts: stored[ts]
            for ts in _within(item, set(stored))
            if stored[ts].thread_ts in (None, ts)
        }
        oldest = min(held.listed, key=MessageTs, default=None)
        self._append_deletes(held, infer_deletes(top_level, held.listed, item.since, oldest))
        return msgspec.structs.replace(item, backfill_complete=True, last_search_at=searched_at)

    def _append_history_page(
        self,
        connection: _Connection,
        held: _HeldConversation,
        page: list[dict],
        view: DirectoryView,
    ) -> None:
        """Appends a page of history and the thread under each parent in it, then flushes."""
        held.listed.update(message["ts"] for message in page)
        parents = [m["ts"] for m in page if (m.get("reply_count") or 0) > 0]
        self._append_read(connection, held, page, parents, view)

    def _reread_threads(
        self,
        connection: _Connection,
        held: _HeldConversation,
        thread_ts: set[str],
        view: DirectoryView,
    ) -> None:
        """Re-reads each of the threads not read yet in this run, oldest first."""
        unread = sorted(thread_ts - held.read_threads, key=MessageTs)
        if unread:
            self._append_read(connection, held, [], unread, view)

    def _append_read(
        self,
        connection: _Connection,
        held: _HeldConversation,
        page: list[dict],
        parents: list[str],
        view: DirectoryView,
    ) -> None:
        """Appends a page of history and the thread under each of parents, then flushes."""
        uow, gateway, team_id, channel = self.uow, connection.gateway, held.team_id, held.channel
        threads: dict[str, list[dict]] = {}
        for ts in parents:
            held.read_threads.add(ts)
            try:
                threads[ts] = gateway.replies(channel, ts)
            except ThreadNotFound:
                self._warn(_missing_message(connection.domain, channel, ts))
        read = [*page, *(m for messages in threads.values() for m in messages)]
        bot_ids = {m["bot_id"] for m in read if m.get("bot_id")} - view.bots.keys()
        bots = _unknown_bots(gateway, team_id, bot_ids, uow.archive)
        append_directory(uow, team_id, "bot", bots, BOTS_API)
        view.bots.update(bots)
        self._append_messages(held, page, HISTORY_API, view)
        for thread_ts, messages in threads.items():
            self._append_thread(held, thread_ts, messages, view)
        uow.archive.flush()

    def _append_messages(
        self, held: _HeldConversation, messages: list[dict], api: str, view: DirectoryView
    ) -> None:
        uow, team_id, channel = self.uow, held.team_id, held.channel
        files = {f["id"]: f for m in messages for f in m.get("files") or []}
        appended = append_directory(uow, team_id, "file", files, api, keep_blob=True)
        held.files.update(appended)
        for message in messages:
            count(uow, MESSAGES_SEEN)
            ts = message["ts"]
            digest = content_hash("message", message)
            stored = held.stored.get(ts)
            if stored is None or stored.hash != digest:
                meta = stamped_meta(
                    uow,
                    "message",
                    team=team_id,
                    channel=channel,
                    ts=ts,
                    source_team=message.get("team") or team_id,
                    api=api,
                    hash=digest,
                    plain=plain_text(message, view),
                )
                append_line(uow, Envelope(meta=meta, object=message))
                held.record(ts, StoredMessage(digest, message.get("thread_ts"), fetched_now(uow)))
            queue_downloads(
                message,
                team_id,
                uow,
                self.options.max_file_bytes,
                view=view,
                files=held.files,
                images=held.images,
                known=held.downloads,
            )

    def _append_thread(
        self, held: _HeldConversation, thread_ts: str, messages: list[dict], view: DirectoryView
    ) -> None:
        self._append_messages(held, messages, REPLIES_API, view)
        returned = {message["ts"] for message in messages}
        members = {ts: held.stored[ts] for ts in held.thread(thread_ts)}
        oldest = min(returned, key=MessageTs, default=None)
        self._append_deletes(held, infer_deletes(members, returned, None, oldest))

    def _append_deletes(self, held: _HeldConversation, stamps: set[str]) -> None:
        uow = self.uow
        for ts in sorted(stamps, key=MessageTs):
            meta = stamped_meta(uow, "deleted", team=held.team_id, channel=held.channel, ts=ts)
            append_line(uow, Envelope(meta=meta))
            held.record(ts, StoredMessage(None, held.stored[ts].thread_ts, fetched_now(uow)))

    def _warn(self, message: str) -> None:
        self.reporter.warn(message)
        self.skipped.append(message)

    def _skip(self, item: TrackedItem, message: str) -> None:
        self._warn(message)
        self._record(msgspec.structs.replace(item, status="not_visible"))

    def _record(self, item: TrackedItem) -> None:
        tracked = self.uow.run.tracked
        tracked[:] = [held for held in tracked if held.key != item.key]
        tracked.append(item)


class _HeldConversation:
    """What the archive holds for one conversation, read once and kept current as lines append.

    listed holds the ts of each message history returned in this run, and read_threads the
    thread_ts of each thread read in this run.
    """

    def __init__(self, archive: ArchiveStore, team_id: str, channel: str) -> None:
        self.team_id = team_id
        self.channel = channel
        self._archive = archive
        self.stored = archive.stored_messages(team_id, channel)
        self.listed: set[str] = set()
        self.read_threads: set[str] = set()
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

    def has_replies(self, thread_ts: str) -> bool:
        return any(ts != thread_ts for ts in self._replies.get(thread_ts, ()))

    def _index(self, ts: str, stored: StoredMessage) -> None:
        if stored.thread_ts is not None:
            self._replies.setdefault(stored.thread_ts, set()).add(ts)


def _within(item: TrackedItem, stamps: set[str]) -> set[str]:
    """The stamps inside the item's since and until bounds, which are exclusive like history's."""
    return {
        ts
        for ts in stamps
        if (item.since is None or MessageTs(ts) > item.since)
        and (item.until is None or MessageTs(ts) < item.until)
    }


def _two_days_before(timestamp: str) -> str:
    """The YYYY-MM-DD date in UTC two days before a fetched_at timestamp.

    Search's after: excludes the named date and reads it in the user's Slack timezone, so one
    day of margin misses replies posted late in the day west of UTC.
    """
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    return (moment - timedelta(days=2)).strftime("%Y-%m-%d")


def _outermost(current: str | None, page: list[dict], *, newest: bool) -> str | None:
    """The newest (or oldest) of current and the ts of each message in page."""
    stamps = [MessageTs(message["ts"]) for message in page]
    if current is not None:
        stamps.append(MessageTs(current))
    if not stamps:
        return None
    return str(max(stamps) if newest else min(stamps))
