# ABOUTME: The pull use case: refreshes the workspace directory and the tracked items it is given.
# ABOUTME: Appends a line only for a new version, records deletes and downloads files and images.
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import msgspec

from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Envelope
from slarc.domain.hashing import content_hash
from slarc.domain.links import (
    ConversationSel,
    MessageSel,
    MessageTs,
    Selector,
    WorkspaceSel,
    message_link,
)
from slarc.domain.plain import plain_text
from slarc.domain.tracking import ItemKey, TrackedItem, item_label
from slarc.ports import (
    ArchiveFailed,
    AuthFailed,
    CommitConflict,
    Downloader,
    ItemProgress,
    NoSession,
    NotVisible,
    Reporter,
    SlackConnector,
    SlarcError,
    StoredMessage,
    ThreadNotFound,
)
from slarc.service.appends import (
    LINES_APPENDED,
    append_directory,
    append_line,
    count,
    counted,
    fetched_now,
    stamped_meta,
)
from slarc.service.directory_refresh import BOTS_API, refresh_directory, unknown_bots
from slarc.service.downloads import drain_downloads, queue_downloads, requeue_skipped
from slarc.service.held_conversation import HeldConversation
from slarc.service.selectors import only_workspace, resolve_conversation
from slarc.service.threads import active_threads, discover_reply_threads, infer_deletes
from slarc.service.workspaces import Connection, Workspaces
from slarc.unit_of_work import UnitOfWork

HISTORY_API = "conversations.history"
REPLIES_API = "conversations.replies"
MESSAGES_SEEN = "messages_seen"
RATE_LIMIT_WAIT = "rate_limit_wait_s"
API_CALLS = "api_calls"
THREADS_READ = "threads_read"

# Why a thread was read: a thread or message link, a parent in history, a search hit, an active
# thread, a --full re-read, or a retry of a read that failed in an earlier pull.
type ThreadReason = Literal["link", "history", "search", "active", "full", "retry"]


@dataclass
class PullOptions:
    since: str | None = None
    until: str | None = None
    full: bool = False
    retry_skipped: bool = False
    channel_mode: bool = False
    downloads: int = 4
    max_file_bytes: int = 100 * 1024 * 1024
    active_thread_days: int = 7


@dataclass
class PullReport:
    """unreachable holds why each workspace that could not connect was skipped.

    messages_new counts the message and deleted lines appended; files_saved and images_saved
    count this run's downloads, and downloads_skipped those too large, forbidden, not found or
    given up.
    not_visible counts the items this run recorded as not visible, whether or not it warned
    about them.
    """

    items_selected: int
    items_pulled: int
    lines_appended: int
    messages_new: int
    messages_seen: int
    skipped: list[str]
    files_saved: int
    images_saved: int
    downloads_pending: int
    downloads_skipped: int
    not_visible: int
    unreachable: list[SlarcError]
    workspaces_pulled: int

    @property
    def failed(self) -> bool:
        """True when no workspace could be pulled or Slack rejected a workspace's credentials."""
        if not self.unreachable:
            return False
        rejected = any(isinstance(error, AuthFailed) for error in self.unreachable)
        return rejected or self.workspaces_pulled == 0


def pull(
    selectors: list[Selector],
    options: PullOptions,
    uow: UnitOfWork,
    connector: SlackConnector,
    downloader: Downloader,
    reporter: Reporter,
) -> PullReport:
    """Pulls the selected items, or every tracked item when selectors is empty.

    A workspace that cannot connect is skipped with a warning and the others are pulled. A read
    that fails for another reason skips one thread or item with a warning.
    """
    run = _PullRun(options, uow, reporter, Workspaces(connector, uow))
    counts = uow.run.counts
    unreachable: list[SlarcError] = []
    try:
        items = run.resolve(selectors)
        teams = list(dict.fromkeys(item.team_id for item in items))
        for team_id in teams:
            try:
                run.pull_workspace(team_id, [i for i in items if i.team_id == team_id])
            except (NoSession, AuthFailed) as error:
                run.skip_workspace(team_id, error)
                unreachable.append(error)
        if options.retry_skipped:
            run.requeue_skipped(items)
        drained = drain_downloads(
            uow,
            teams,
            downloader,
            options.downloads,
            options.max_file_bytes,
            run.workspaces,
            reporter,
        )
    finally:
        counts[RATE_LIMIT_WAIT] = round(run.workspaces.rate_limit_wait_s(), 3)
        counts[API_CALLS] = run.workspaces.api_calls()
        counts[THREADS_READ] = dict(run.threads_read)
    return PullReport(
        items_selected=len(items),
        items_pulled=run.pulled,
        lines_appended=counted(uow, LINES_APPENDED),
        messages_new=run.messages_new,
        messages_seen=counted(uow, MESSAGES_SEEN),
        skipped=run.skipped,
        files_saved=drained.files_saved,
        images_saved=drained.images_saved,
        downloads_pending=drained.pending,
        downloads_skipped=drained.skipped,
        not_visible=run.not_visible,
        unreachable=unreachable,
        workspaces_pulled=len(teams) - len(unreachable),
    )


def date_to_ts(date: str, end_of_day: bool) -> str:
    """The ts at the start of a YYYY-MM-DD day in UTC, or with end_of_day the start of the next.

    History bounds are exclusive, so the next day's start ends the range after the whole day.
    """
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    if end_of_day:
        start += timedelta(days=1)
    return f"{int(start.timestamp())}.000000"


def _missing_message(domain: str, channel: str, ts: str) -> str:
    return f"Slack returns no message at {message_link(domain, channel, ts)}; skipping it."


class _PullRun:
    def __init__(
        self,
        options: PullOptions,
        uow: UnitOfWork,
        reporter: Reporter,
        workspaces: Workspaces,
    ) -> None:
        self.options = options
        self.uow = uow
        self.reporter = reporter
        self.workspaces = workspaces
        self.prefetched: dict[ItemKey, list[dict]] = {}
        self.skipped: list[str] = []
        self.progress: _ItemProgress | None = None
        self.threads_read: dict[str, int] = {}
        self.pulled = 0
        self.messages_new = 0
        self.not_visible = 0
        self._threads_counted: set[tuple[str, str, str]] = set()

    def requeue_skipped(self, items: list[TrackedItem]) -> None:
        try:
            requeue_skipped(self.uow, items, self.options.max_file_bytes)
        except CommitConflict as error:
            self._warn(f"Could not queue skipped downloads again: {error}")

    def resolve(self, selectors: list[Selector]) -> list[TrackedItem]:
        """The items to pull, each committed before it is read; a thread read for its kind is
        kept. An item whose commit conflicts is left out with a warning."""
        tracked = self.uow.archive.tracked_items()
        if not selectors:
            return list(tracked.values())
        items: dict[ItemKey, TrackedItem] = {}
        for selector in selectors:
            connection = self.workspaces.connection_to(selector.domain or only_workspace(tracked))
            if isinstance(selector, WorkspaceSel):
                held = [item for item in tracked.values() if item.team_id == connection.team_id]
                if not held:
                    raise SlarcError(
                        f"Nothing is tracked in workspace {selector.domain}. "
                        "Pull a Slack link from it to start tracking it."
                    )
                for item in held:
                    items.setdefault(item.key, item)
                continue
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
            if item != known:
                try:
                    self._record(item)
                except CommitConflict as error:
                    name = _item_name(connection, item)
                    self._warn(f"Could not track {name}, so it was not pulled: {error}")
                    continue
            items.setdefault(item.key, item)
        return list(items.values())

    def _ranged(self, item: TrackedItem, known: TrackedItem | None) -> TrackedItem:
        """A new conversation takes the options' date range; a tracked one only widens to it.

        Widening since reopens the backfill, which then pages the gap below oldest_ts. Widening
        until clears archived, so the next pull reads the new range even if Slack has archived
        the conversation.
        """
        since = date_to_ts(self.options.since, end_of_day=False) if self.options.since else None
        until = date_to_ts(self.options.until, end_of_day=True) if self.options.until else None
        if known is None:
            return msgspec.structs.replace(item, since=since, until=until)
        if since is not None and known.since is not None and MessageTs(since) < known.since:
            known = msgspec.structs.replace(known, since=since, backfill_complete=False)
        if until is not None and known.until is not None and MessageTs(until) > known.until:
            known = msgspec.structs.replace(known, until=until, archived=False)
        return known

    def _message_item(
        self, selector: MessageSel, connection: Connection, tracked: dict[ItemKey, TrackedItem]
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
        except AuthFailed, ArchiveFailed:
            raise
        except SlarcError as error:
            link = message_link(connection.domain, channel, ts)
            self._warn(f"Could not read {link}; pull it again to track it: {error}")
            return None
        kind = "thread" if len(messages) > 1 else "message"
        item = TrackedItem(team_id=team_id, kind=kind, channel=channel, ts=messages[0]["ts"])
        self._count_thread_read(team_id, channel, messages[0]["ts"], "link")
        self.prefetched[item.key] = messages
        return item

    def pull_workspace(self, team_id: str, items: list[TrackedItem]) -> None:
        """Reads the thread items, commits the directory refresh, then pulls each item.

        A refresh commit that conflicts means the workspace was removed meanwhile, so its
        items are skipped with a warning. A thread item whose commit conflicts is skipped.
        """
        connection = self.workspaces.connection_to(team_id)
        conversations = [item for item in items if item.kind == "conversation"]
        threads = [item for item in items if item.kind != "conversation"]
        reads: dict[ItemKey, list[dict]] = {}
        for item in threads:
            try:
                messages = self._read_thread(connection, item)
            except CommitConflict as error:
                self._unfinished(_item_name(connection, item), error)
                continue
            if messages is not None:
                reads[item.key] = messages
        bot_ids = {m["bot_id"] for messages in reads.values() for m in messages if m.get("bot_id")}
        channels = {item.channel for item in threads if item.key in reads}
        channels |= {item.channel for item in conversations}
        view = refresh_directory(connection.gateway, team_id, channels, self.uow, bot_ids)
        try:
            self.uow.commit()
        except CommitConflict:
            self._warn(
                f"Workspace {team_id} was removed while this pull was running, so its tracked "
                "items were not pulled."
            )
            return
        for item in threads:
            if item.key not in reads:
                continue
            held = HeldConversation(self.uow.archive, team_id, item.channel)
            label = item_label(item, view)
            self.progress = _ItemProgress(self.reporter, label, report_start=False)
            try:
                self._append_thread(held, item.ts or "", reads[item.key], view)
                self._finish(connection, held, item, view)
            except CommitConflict as error:
                self._unfinished(label, error)
        for item in conversations:
            self._pull_conversation(connection, item, view)

    def _finish(
        self,
        connection: Connection,
        held: HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
    ) -> None:
        last_read = dict(item.last_read)
        conversation = view.conversations.get(item.channel, {})
        if conversation.get("last_read"):
            last_read[connection.user_id] = conversation["last_read"]
        archived = (
            item.kind == "conversation"
            and bool(conversation.get("is_archived"))
            and not held.retry_threads(item)
        )
        self._record(
            msgspec.structs.replace(
                item,
                status="ok",
                last_pull_at=fetched_now(self.uow),
                last_read=last_read,
                archived=archived,
            ),
            held,
        )
        self.pulled += 1
        self._end_progress()

    def _read_thread(self, connection: Connection, item: TrackedItem) -> list[dict] | None:
        """The thread's messages, parent first, or None when the item cannot be read.

        A thread Slack no longer returns is skipped rather than recorded as deleted, since a
        session that cannot see it gets the same answer.
        """
        if item.key in self.prefetched:
            return self.prefetched[item.key]
        ts = item.ts or ""
        try:
            messages = connection.gateway.replies(item.channel, ts)
        except NotVisible as error:
            self._skip(item, str(error))
            return None
        except ThreadNotFound:
            self._skip(item, _missing_message(connection.domain, item.channel, ts))
            return None
        except AuthFailed, ArchiveFailed:
            raise
        except SlarcError as error:
            link = message_link(connection.domain, item.channel, ts)
            self._warn(f"Could not read {link}; the next pull tries again: {error}")
            return None
        self._count_thread_read(item.team_id, item.channel, ts, "link")
        return messages

    def _pull_conversation(
        self, connection: Connection, item: TrackedItem, view: DirectoryView
    ) -> None:
        """Pulls the conversation item. When the pull fails, including a commit that conflicts,
        what it has not committed is discarded with a warning, and the next pull resumes it."""
        held = HeldConversation(self.uow.archive, item.team_id, item.channel)
        self.progress = _ItemProgress(self.reporter, item_label(item, view))
        try:
            self._read_conversation(connection, held, item, view)
        except AuthFailed, ArchiveFailed:
            raise
        except SlarcError as error:
            self.uow.discard()
            self._unfinished(f"conversation {item.channel}", error)

    def _read_conversation(
        self, connection: Connection, held: HeldConversation, item: TrackedItem, view: DirectoryView
    ) -> None:
        """Pulls new history and new replies under older parents, then re-reads active threads.

        Threads whose read failed in an earlier pull are re-read first. --full re-reads the
        whole range instead of history and search. A first pull's history covers every reply, so
        it skips search and later pulls search from its start. Any read failing with NotVisible
        skips the item without deletes; a history page that fails otherwise leaves the item
        unfinished, keeping the pages already committed.

        An item recorded as archived whose conversation is still archived and whose backfill is
        complete has had its final refresh, so it is recorded as pulled without reading
        anything, unless --full is set.
        """
        if (
            item.archived
            and item.backfill_complete
            and view.conversations.get(item.channel, {}).get("is_archived")
            and not self.options.full
        ):
            self._finish(connection, held, item, view)
            return
        started_at_top = not item.backfill_complete and item.oldest_ts is None
        if started_at_top:
            item = msgspec.structs.replace(item, last_search_at=fetched_now(self.uow))
        try:
            retries = _within(item, set(item.retry_threads))
            self._reread_threads(connection, held, retries, view, "retry")
            if self.options.full and not started_at_top:
                item = self._reread_range(connection, held, item, view)
            else:
                item = self._pull_history(connection, held, item, view, started_at_top)
                if not started_at_top:
                    item = self._search_replies(connection, held, item, view)
            now, days = self.uow.clock.now(), self.options.active_thread_days
            active = active_threads(held.stored, now, days)
            self._reread_threads(connection, held, _within(item, active), view, "active")
        except NotVisible as error:
            self._skip(item, str(error), held)
            return
        self._finish(connection, held, item, view)

    def _pull_history(
        self,
        connection: Connection,
        held: HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
        started_at_top: bool,
    ) -> TrackedItem:
        """Pages older history while the backfill is incomplete, then newer than newest_ts."""
        if not item.backfill_complete:
            latest = item.oldest_ts or item.until
            item = self._page_history(
                connection, held, item, view, item.since, latest, "history", newest_each_page=True
            )
        if not started_at_top:
            oldest = item.newest_ts or item.since
            item = self._page_history(
                connection, held, item, view, oldest, item.until, "history", newest_each_page=False
            )
        return item

    def _page_history(
        self,
        connection: Connection,
        held: HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
        oldest: str | None,
        latest: str | None,
        reason: ThreadReason,
        *,
        newest_each_page: bool,
    ) -> TrackedItem:
        """Pages history from oldest up to latest, reading each parent's thread for reason.

        The item is committed after every page with oldest_ts moved. With newest_each_page, the
        backfill, newest_ts moves with each page, so an interrupted pull resumes below it, and
        the backfill is marked complete at the end. Otherwise pages come newest first, so
        newest_ts moves only once paging ends; an interrupted pull then pages the same range
        again.
        """
        newest = item.newest_ts
        if self.progress is not None:
            top = item.until or f"{self.uow.clock.now().timestamp():.6f}"
            self.progress.paging(top, _history_floor(oldest, view, item.channel))
        for page in connection.gateway.history(item.channel, oldest, latest):
            self._append_history_page(connection, held, page, view, reason)
            newest = _outermost(newest, page, newest=True)
            oldest_ts = _outermost(item.oldest_ts, page, newest=False)
            item = msgspec.structs.replace(item, oldest_ts=oldest_ts)
            if newest_each_page:
                item = msgspec.structs.replace(item, newest_ts=newest)
            self._record(item, held)
        if not newest_each_page:
            return msgspec.structs.replace(item, newest_ts=newest)
        item = msgspec.structs.replace(item, backfill_complete=True)
        self._record(item, held)
        return item

    def _search_replies(
        self,
        connection: Connection,
        held: HeldConversation,
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
            self._reread_threads(connection, held, _within(item, threads), view, "search")
        return msgspec.structs.replace(item, last_search_at=searched_at)

    def _reread_range(
        self,
        connection: Connection,
        held: HeldConversation,
        item: TrackedItem,
        view: DirectoryView,
    ) -> TrackedItem:
        """Re-reads the whole range and every thread in it, then records deleted messages.

        Each thread re-read records its own deleted replies, so the range covers top-level
        messages only.
        """
        searched_at = fetched_now(self.uow)
        item = self._page_history(
            connection, held, item, view, item.since, item.until, "full", newest_each_page=False
        )
        parents = {ts for ts in held.listed if held.has_replies(ts)}
        self._reread_threads(connection, held, parents, view, "full")
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
        connection: Connection,
        held: HeldConversation,
        page: list[dict],
        view: DirectoryView,
        reason: ThreadReason,
    ) -> None:
        """Appends a page of history and the thread under each parent in it."""
        held.listed.update(message["ts"] for message in page)
        parents = [m["ts"] for m in page if (m.get("reply_count") or 0) > 0]
        self._append_read(connection, held, page, parents, view, reason)

    def _reread_threads(
        self,
        connection: Connection,
        held: HeldConversation,
        thread_ts: set[str],
        view: DirectoryView,
        reason: ThreadReason,
    ) -> None:
        """Re-reads each of the threads not read yet in this run, oldest first."""
        unread = sorted(thread_ts - held.read_threads, key=MessageTs)
        if unread:
            self._append_read(connection, held, [], unread, view, reason)

    def _append_read(
        self,
        connection: Connection,
        held: HeldConversation,
        page: list[dict],
        parents: list[str],
        view: DirectoryView,
        reason: ThreadReason,
    ) -> None:
        """Appends a page of history, then reads and appends the thread under each of parents.

        Progress is reported after the page and after each thread. Under a page, the oldest
        message reached moves to each parent as its thread is read, then to the page's oldest
        message, which a page without threads reaches at once. A thread whose
        read fails with NotVisible or a plain SlarcError is kept in held.failed_threads;
        NotVisible then propagates, and a plain SlarcError skips the thread with a warning.
        AuthFailed and ArchiveFailed propagate.
        """
        oldest = _outermost(None, page, newest=False)
        if page:
            if not parents:
                self._reach(oldest)
            self._append_bots(connection, page, view)
            self._append_messages(held, page, HISTORY_API, view)
            self._report_progress()
        gateway, channel = connection.gateway, held.channel
        for ts in parents:
            if page:
                self._reach(ts)
            held.read_threads.add(ts)
            try:
                messages = gateway.replies(channel, ts)
            except ThreadNotFound:
                self._warn(_missing_message(connection.domain, channel, ts))
                continue
            except NotVisible:
                held.failed_threads.add(ts)
                raise
            except AuthFailed, ArchiveFailed:
                raise
            except SlarcError as error:
                link = message_link(connection.domain, channel, ts)
                self._warn(
                    f"Could not read the thread at {link}, so it is retried on the next pull: "
                    f"{error}"
                )
                held.failed_threads.add(ts)
                continue
            self._count_thread_read(held.team_id, channel, ts, reason)
            self._append_bots(connection, messages, view)
            self._append_thread(held, ts, messages, view)
            self._report_progress()
        if page and parents:
            self._reach(oldest)
            self._report_progress()

    def _reach(self, ts: str | None) -> None:
        """Moves the oldest message reached in history paging to ts."""
        if self.progress is not None:
            self.progress.at = ts

    def _append_bots(
        self, connection: Connection, messages: list[dict], view: DirectoryView
    ) -> None:
        """Appends the bots the messages name that the view does not know yet."""
        team_id = connection.team_id
        bot_ids = {m["bot_id"] for m in messages if m.get("bot_id")} - view.bots.keys()
        bots = unknown_bots(connection.gateway, team_id, bot_ids, self.uow.archive)
        append_directory(self.uow, team_id, "bot", bots, BOTS_API)
        view.bots.update(bots)

    def _report_progress(self) -> None:
        if self.progress is not None:
            self.progress.report()

    def _append_messages(
        self, held: HeldConversation, messages: list[dict], api: str, view: DirectoryView
    ) -> None:
        uow, team_id, channel = self.uow, held.team_id, held.channel
        files = {f["id"]: f for m in messages for f in m.get("files") or [] if f.get("id")}
        appended = append_directory(uow, team_id, "file", files, api, keep_blob=True)
        held.files.update(appended)
        for message in messages:
            ts = message["ts"]
            self._count_seen(held, ts)
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
                self._count_new()
            queue_downloads(
                message,
                team_id,
                uow,
                self.options.max_file_bytes,
                view=view,
                files=held.files,
                images=held.images,
                known=held.downloads,
                retry_skipped=self.options.full,
            )

    def _append_thread(
        self, held: HeldConversation, thread_ts: str, messages: list[dict], view: DirectoryView
    ) -> None:
        self._append_messages(held, messages, REPLIES_API, view)
        returned = {message["ts"] for message in messages}
        members = {ts: held.stored[ts] for ts in held.thread(thread_ts)}
        oldest = min(returned, key=MessageTs, default=None)
        self._append_deletes(held, infer_deletes(members, returned, None, oldest))

    def _append_deletes(self, held: HeldConversation, stamps: set[str]) -> None:
        uow = self.uow
        for ts in sorted(stamps, key=MessageTs):
            meta = stamped_meta(uow, "deleted", team=held.team_id, channel=held.channel, ts=ts)
            append_line(uow, Envelope(meta=meta))
            held.record(ts, StoredMessage(None, held.stored[ts].thread_ts, fetched_now(uow)))
            self._count_new()

    def _count_seen(self, held: HeldConversation, ts: str) -> None:
        """Counts a message read, for the run and the item in progress, the first time this run
        reads it in the conversation."""
        if ts in held.counted:
            return
        held.counted.add(ts)
        count(self.uow, MESSAGES_SEEN)
        if self.progress is not None:
            self.progress.seen += 1

    def _count_new(self) -> None:
        """Counts a message or deleted line appended, for the run and the item in progress."""
        self.messages_new += 1
        if self.progress is not None:
            self.progress.new += 1

    def skip_workspace(self, team_id: str, error: SlarcError) -> None:
        """Discards what the workspace's pull has not committed, leaves its tracked items as the
        last commit left them, and records why in the run row."""
        self.uow.discard()
        self._end_progress()
        self._warn(
            f"Could not connect to workspace {team_id}, so its tracked items were not pulled: "
            f"{error}"
        )
        self.uow.run.errors.append({"team_id": team_id, "error": str(error)})

    def _warn(self, message: str) -> None:
        self.reporter.warn(message)
        self.skipped.append(message)

    def _unfinished(self, name: str, error: SlarcError) -> None:
        self._warn(f"Could not finish pulling {name}; the next pull resumes it: {error}")
        self._end_progress()

    def _skip(self, item: TrackedItem, message: str, held: HeldConversation | None = None) -> None:
        if item.status == "ok":
            self._warn(message)
        self.not_visible += 1
        self._record(msgspec.structs.replace(item, status="not_visible"), held)
        self._end_progress()

    def _end_progress(self) -> None:
        if self.progress is not None:
            self.progress.end()
            self.progress = None

    def _count_thread_read(
        self, team_id: str, channel: str, thread_ts: str, reason: ThreadReason
    ) -> None:
        """Counts a thread read under its reason, once per thread in the run."""
        key = (team_id, channel, thread_ts)
        if key not in self._threads_counted:
            self._threads_counted.add(key)
            self.threads_read[reason] = self.threads_read.get(reason, 0) + 1

    def _record(self, item: TrackedItem, held: HeldConversation | None = None) -> None:
        """Commits the open batch with the item; with its held conversation, the item also
        takes the threads to retry."""
        if held is not None:
            item = msgspec.structs.replace(item, retry_threads=held.retry_threads(item))
        self.uow.commit([item])


class _ItemProgress:
    """Reports how many messages have been read for one item and how many lines were new for it,
    and while history is paged, the oldest message reached and the part of the range read."""

    def __init__(self, reporter: Reporter, label: str, *, report_start: bool = True) -> None:
        self.reporter = reporter
        self.label = label
        self.seen = 0
        self.new = 0
        self.at: str | None = None
        self.target: str | None = None
        self.top: str | None = None
        if report_start:
            self.report()

    def paging(self, top: str, target: str | None) -> None:
        """History is paged from top, the newest ts in range, back to target."""
        self.top, self.target = top, target

    def report(self) -> None:
        self._send(self._share())

    def end(self) -> None:
        self._send(1)

    def _send(self, share: float | None) -> None:
        update = ItemProgress(self.seen, self.new, self.at, self.target, share)
        self.reporter.progress(self.label, update)

    def _share(self) -> float | None:
        if self.at is None or self.target is None or self.top is None:
            return None
        span = float(self.top) - float(self.target)
        if span <= 0:
            return None
        return min(max((float(self.top) - float(self.at)) / span, 0.0), 1.0)


def _item_name(connection: Connection, item: TrackedItem) -> str:
    """How a warning names an item before the directory is read: a conversation by its id, a
    thread or message by its link."""
    if item.kind == "conversation":
        return f"conversation {item.channel}"
    return message_link(connection.domain, item.channel, item.ts or "")


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


def _history_floor(oldest: str | None, view: DirectoryView, channel: str) -> str | None:
    """The ts history pages back to: oldest, or the conversation's creation when later."""
    created = view.conversations.get(channel, {}).get("created")
    floors = [MessageTs(ts) for ts in (oldest, str(created) if created else None) if ts]
    return str(max(floors)) if floors else None


def _outermost(current: str | None, page: list[dict], *, newest: bool) -> str | None:
    """The newest (or oldest) of current and the ts of each message in page."""
    stamps = [MessageTs(message["ts"]) for message in page]
    if current is not None:
        stamps.append(MessageTs(current))
    if not stamps:
        return None
    return str(max(stamps) if newest else min(stamps))
