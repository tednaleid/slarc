# ABOUTME: The pull use case: refreshes the workspace directory and the tracked items it is given.
# ABOUTME: Appends a line only for a new version, records deletes and queues file downloads.
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import msgspec

from slarc import __version__
from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Envelope, Kind, Meta, fetched_at
from slarc.domain.hashing import content_hash
from slarc.domain.links import ConversationSel, MessageSel, MessageTs, Selector, message_link
from slarc.domain.plain import plain_text
from slarc.domain.tracking import TrackedItem
from slarc.ports import (
    ArchiveStore,
    AuthFailed,
    Downloader,
    NotVisible,
    Reporter,
    SlackConnector,
    SlackGateway,
    SlarcError,
    ThreadNotFound,
)
from slarc.service.auth import domain_of
from slarc.service.downloads import queue_downloads
from slarc.unit_of_work import UnitOfWork

REPLIES_API = "conversations.replies"
LINES_APPENDED = "lines_appended"
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
    counts = uow.run.counts
    return PullReport(
        lines_appended=int(counts.get(LINES_APPENDED, 0)),
        messages_seen=int(counts.get(MESSAGES_SEEN, 0)),
        skipped=run.skipped,
        downloads_pending=sum(1 for d in uow.run.downloads if d.status == "pending"),
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
    bots = {}
    for bot_id in sorted(set(bot_ids) - archive.directory_state(team_id, "bot").keys()):
        try:
            bots[bot_id] = gateway.bot_info(bot_id)
        except AuthFailed:
            raise
        except SlarcError:
            # A bot Slack no longer knows still renders from the message's bot_profile.
            continue
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
        ("bot", "bots.info", bots),
        ("conversation", "conversations.info", conversations),
    ]
    for kind, api, objects in fresh:
        _append_directory(uow, team_id, kind, objects, api)

    def current(kind: Kind, objects: dict[str, dict]) -> dict[str, dict]:
        return archive.directory_objects(team_id, kind) | objects

    return DirectoryView(
        users=current("user", users),
        conversations=current("conversation", conversations),
        usergroups=current("usergroup", usergroups),
        emoji={name: obj.get("url", "") for name, obj in current("emoji", emoji).items()},
        bots=current("bot", bots),
    )


def resolve_conversation(
    selector: ConversationSel, team_id: str, gateway: SlackGateway, archive: ArchiveStore
) -> str:
    """The channel id a selector names: by id, by #name, or the DM with @user."""
    if selector.channel is not None:
        return selector.channel
    if selector.name is not None:
        name = selector.name

        def named(conversation: dict) -> bool:
            return conversation.get("name") == name

        found = _find_id(archive.directory_objects(team_id, "conversation").values(), named)
        found = found or _find_id(gateway.list_conversations(), named)
        if found is None:
            raise SlarcError(f"No conversation named #{name} was found in workspace {team_id}.")
        return found
    if selector.dm_user is not None:
        user_id = _resolve_user(selector.dm_user, team_id, gateway, archive)

        def dm_with_user(conversation: dict) -> bool:
            return bool(conversation.get("is_im")) and conversation.get("user") == user_id

        found = _find_id(gateway.list_conversations(), dm_with_user)
        if found is None:
            raise SlarcError(f"No DM with @{selector.dm_user} was found in workspace {team_id}.")
        return found
    raise ValueError("The selector names no conversation.")


@dataclass(frozen=True)
class _Connection:
    team_id: str
    user_id: str
    domain: str
    gateway: SlackGateway


class _Workspaces:
    """Connects once per workspace in a run, since each connection has its own rate limiter.

    Each connection is recorded in the run's teams and auth entries.
    """

    def __init__(self, connector: SlackConnector, uow: UnitOfWork) -> None:
        self._connector = connector
        self._uow = uow
        self._team_of: dict[str, str] = {}
        self._connections: dict[str, _Connection] = {}

    def connect(self, workspace: str) -> _Connection:
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
                "auth_test_at": _now(self._uow),
            }
        )
        return _Connection(info["team_id"], info["user_id"], domain, gateway)


def _only_workspace(tracked: dict[ItemKey, TrackedItem]) -> str:
    teams = {team_id for team_id, _, _, _ in tracked}
    if len(teams) == 1:
        return teams.pop()
    held = "no workspace yet" if not teams else "several workspaces"
    raise SlarcError(f"This archive holds {held}, so use a Slack link to say which to pull from.")


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
            connection = self.workspaces.connect(selector.domain or _only_workspace(tracked))
            if isinstance(selector, ConversationSel):
                channel = resolve_conversation(
                    selector, connection.team_id, connection.gateway, self.uow.archive
                )
                item = TrackedItem(team_id=connection.team_id, kind="conversation", channel=channel)
            else:
                item = self._message_item(selector, connection, tracked)
            if item is not None:
                items.setdefault(item.key, tracked.get(item.key, item))
        return list(items.values())

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
        connection = self.workspaces.connect(team_id)
        reads: dict[ItemKey, list[dict]] = {}
        for item in items:
            if item.kind == "conversation":
                raise NotImplementedError("Pulling a whole conversation is not supported.")
            messages = self._read_thread(connection, item)
            if messages is not None:
                reads[item.key] = messages
        bot_ids = {m["bot_id"] for messages in reads.values() for m in messages if m.get("bot_id")}
        channels = {item.channel for item in items if item.key in reads}
        view = refresh_directory(connection.gateway, team_id, channels, self.uow, bot_ids)
        for item in items:
            if item.key not in reads:
                continue
            self._append_thread(item, reads[item.key], view)
            last_read = dict(item.last_read)
            conversation = view.conversations.get(item.channel, {})
            if conversation.get("last_read"):
                last_read[connection.user_id] = conversation["last_read"]
            self._record(
                msgspec.structs.replace(
                    item, status="ok", last_pull_at=_now(self.uow), last_read=last_read
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

    def _append_thread(self, item: TrackedItem, messages: list[dict], view: DirectoryView) -> None:
        uow, team_id, channel = self.uow, item.team_id, item.channel
        files = {f["id"]: f for m in messages for f in m.get("files") or []}
        _append_directory(uow, team_id, "file", files, REPLIES_API, keep_blob=True)
        stored = uow.archive.stored_messages(team_id, channel)
        for message in messages:
            _count(uow, MESSAGES_SEEN)
            digest = content_hash("message", message)
            held = stored.get(message["ts"])
            if held is None or held.hash != digest:
                meta = _meta(
                    uow,
                    "message",
                    team=team_id,
                    channel=channel,
                    ts=message["ts"],
                    source_team=message.get("team") or team_id,
                    api=REPLIES_API,
                    hash=digest,
                    plain=plain_text(message, view),
                )
                _append(uow, Envelope(meta=meta, object=message))
            queue_downloads(message, team_id, uow, self.options.max_file_bytes)
        returned = {message["ts"] for message in messages}
        # Slack may stop serving the oldest messages, so only those at or after the oldest one
        # returned can be recorded as deleted.
        oldest = min((MessageTs(ts) for ts in returned), default=None)
        for ts, held in stored.items():
            in_range = oldest is not None and MessageTs(ts) >= oldest
            in_thread = item.ts in (ts, held.thread_ts)
            if in_thread and in_range and held.hash is not None and ts not in returned:
                meta = _meta(uow, "deleted", team=team_id, channel=channel, ts=ts)
                _append(uow, Envelope(meta=meta))

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


def _append_directory(
    uow: UnitOfWork,
    team_id: str,
    kind: Kind,
    objects: dict[str, dict],
    api: str,
    *,
    keep_blob: bool = False,
) -> None:
    """Appends each object whose hash differs from its stored line.

    keep_blob carries the stored blob onto the new line: an inline file object and the
    files.info one differ in a few fields but describe the same downloaded bytes.
    """
    if not objects:
        return
    state = uow.archive.directory_state(team_id, kind)
    appended = False
    for id_, obj in objects.items():
        digest = content_hash(kind, obj)
        held = state.get(id_)
        if held is not None and held.hash == digest:
            continue
        blob = held.blob if keep_blob and held is not None else None
        meta = _meta(uow, kind, team=team_id, id=id_, api=api, hash=digest, blob=blob)
        _append(uow, Envelope(meta=meta, object=obj))
        appended = True
    if appended:
        # Directory state is read back from the store, which must see these lines.
        uow.archive.flush()


def _resolve_user(handle: str, team_id: str, gateway: SlackGateway, archive: ArchiveStore) -> str:
    def matches(user: dict) -> bool:
        return handle in (user.get("name"), (user.get("profile") or {}).get("display_name"))

    found = _find_id(archive.directory_objects(team_id, "user").values(), matches)
    found = found or _find_id(gateway.users(), matches)
    if found is None:
        raise SlarcError(f"No user @{handle} was found in workspace {team_id}.")
    return found


def _find_id(objects: Iterable[dict], predicate: Callable[[dict], bool]) -> str | None:
    return next((obj["id"] for obj in objects if predicate(obj)), None)


def _now(uow: UnitOfWork) -> str:
    return fetched_at(uow.clock.now())


def _meta(uow: UnitOfWork, kind: Kind, **fields: str | None) -> Meta:
    stamped = Meta(kind=kind, fetched_at=_now(uow), run=uow.run.run, slarc=__version__)
    return msgspec.structs.replace(stamped, **fields)


def _append(uow: UnitOfWork, env: Envelope) -> None:
    uow.archive.append(env)
    _count(uow, LINES_APPENDED)


def _count(uow: UnitOfWork, name: str) -> None:
    uow.run.counts[name] = uow.run.counts.get(name, 0) + 1
