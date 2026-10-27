# ABOUTME: MessageWriter appends message lines, deleted lines and the users and bots messages
# ABOUTME: name, for pull and listen, counting each message seen and each line appended.
from collections.abc import Callable

from slarc.domain.directory import DirectoryView, referenced_users
from slarc.domain.envelope import Envelope
from slarc.domain.hashing import content_hash
from slarc.domain.links import MessageTs
from slarc.domain.plain import plain_text
from slarc.ports import StoredMessage
from slarc.service.appends import (
    append_directory,
    append_line,
    count,
    fetched_now,
    stamped_meta,
)
from slarc.service.directory_refresh import BOTS_API, USERS_API, unknown_bots, unknown_users
from slarc.service.downloads import queue_downloads
from slarc.service.held_conversation import HeldConversation
from slarc.service.threads import infer_deletes
from slarc.service.workspaces import Connection
from slarc.unit_of_work import UnitOfWork

REPLIES_API = "conversations.replies"
MESSAGES_SEEN = "messages_seen"


def _nothing() -> None:
    pass


class MessageWriter:
    """Appends to a unit of work's open batch. on_seen runs the first time a run counts a
    message in a conversation, and on_new for each message or deleted line appended."""

    def __init__(
        self,
        uow: UnitOfWork,
        max_file_bytes: int,
        *,
        retry_skipped: bool = False,
        on_seen: Callable[[], None] = _nothing,
        on_new: Callable[[], None] = _nothing,
    ) -> None:
        self.uow = uow
        self.max_file_bytes = max_file_bytes
        self.retry_skipped = retry_skipped
        self.on_seen = on_seen
        self.on_new = on_new
        self._missing_users: set[str] = set()

    def append_unknown(
        self, connection: Connection, messages: list[dict], view: DirectoryView
    ) -> None:
        """Appends the bots and users the messages name that the view does not know yet."""
        team_id, gateway = connection.team_id, connection.gateway
        bot_ids = {m["bot_id"] for m in messages if m.get("bot_id")} - view.bots.keys()
        bots = unknown_bots(gateway, team_id, bot_ids, self.uow.archive)
        append_directory(self.uow, team_id, "bot", bots, BOTS_API)
        view.bots.update(bots)
        user_ids = referenced_users(messages) - view.users.keys()
        users = unknown_users(gateway, user_ids, self._missing_users)
        append_directory(self.uow, team_id, "user", users, USERS_API)
        view.users.update(users)

    def append_messages(
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
                self.on_new()
            queue_downloads(
                message,
                team_id,
                uow,
                self.max_file_bytes,
                view=view,
                files=held.files,
                images=held.images,
                known=held.downloads,
                retry_skipped=self.retry_skipped,
            )

    def append_thread(
        self, held: HeldConversation, thread_ts: str, messages: list[dict], view: DirectoryView
    ) -> None:
        """Appends a thread as conversations.replies returned it, and a deleted line for each
        stored member it no longer returns."""
        self.append_messages(held, messages, REPLIES_API, view)
        returned = {message["ts"] for message in messages}
        members = {ts: held.stored[ts] for ts in held.thread(thread_ts)}
        oldest = min(returned, key=MessageTs, default=None)
        self.append_deletes(held, infer_deletes(members, returned, None, oldest))

    def append_deletes(self, held: HeldConversation, stamps: set[str]) -> None:
        uow = self.uow
        for ts in sorted(stamps, key=MessageTs):
            meta = stamped_meta(uow, "deleted", team=held.team_id, channel=held.channel, ts=ts)
            append_line(uow, Envelope(meta=meta))
            held.record(ts, StoredMessage(None, held.stored[ts].thread_ts, fetched_now(uow)))
            self.on_new()

    def _count_seen(self, held: HeldConversation, ts: str) -> None:
        """Counts a message read, the first time this run reads it in the conversation."""
        if ts in held.counted:
            return
        held.counted.add(ts)
        count(self.uow, MESSAGES_SEEN)
        self.on_seen()
