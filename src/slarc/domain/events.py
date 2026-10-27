# ABOUTME: Turns an RTM event into the change it asks of the archive: a message to store, a delete,
# ABOUTME: a message to re-read, or a changed user or emoji; events about read state change nothing.
from dataclasses import dataclass

ENVELOPE_KEYS = ("channel", "event_ts", "suppress_notification", "source_team", "user_team")


@dataclass(frozen=True)
class StoreMessage:
    channel: str
    message: dict


@dataclass(frozen=True)
class DeleteMessage:
    channel: str
    ts: str


@dataclass(frozen=True)
class Reread:
    channel: str
    ts: str


@dataclass(frozen=True)
class UserChanged:
    user: dict


@dataclass(frozen=True)
class EmojiChanged:
    pass


type Change = StoreMessage | DeleteMessage | Reread | UserChanged | EmojiChanged


def classify(event: dict) -> Change | None:
    """The change an event asks for, or None for an event that changes nothing archived."""
    match event.get("type"):
        case "message":
            return _message_change(event)
        case "reaction_added" | "reaction_removed":
            item = event.get("item") or {}
            if item.get("type") == "message" and item.get("channel") and item.get("ts"):
                return Reread(item["channel"], item["ts"])
            return None
        case "user_change" if isinstance(event.get("user"), dict):
            return UserChanged(event["user"])
        case "emoji_changed":
            return EmojiChanged()
        case _:
            return None


def _message_change(event: dict) -> Change | None:
    channel = event.get("channel")
    if not channel:
        return None
    match event.get("subtype"):
        case "message_changed" if isinstance(event.get("message"), dict):
            return StoreMessage(channel, _without_envelope(event["message"]))
        case "message_deleted" if event.get("deleted_ts"):
            return DeleteMessage(channel, event["deleted_ts"])
        case _ if event.get("hidden") or not event.get("ts"):
            return None
        case _:
            return StoreMessage(channel, _without_envelope(event))


def _without_envelope(message: dict) -> dict:
    return {key: value for key, value in message.items() if key not in ENVELOPE_KEYS}
