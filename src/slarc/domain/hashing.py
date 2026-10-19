# ABOUTME: Content hash of the hashed subset of a Slack object, per envelope kind.
# ABOUTME: Per-user and per-request fields are excluded, so unchanged content hashes the same.
import hashlib
import json
from collections.abc import Callable

from slarc.domain.envelope import Kind

_MESSAGE_KEYS = (
    "text",
    "blocks",
    "attachments",
    "edited",
    "reactions",
    "subtype",
    "user",
    "bot_id",
)
_USER_KEYS = ("profile", "name", "real_name", "deleted", "tz", "is_bot", "is_app_user")
_CONVERSATION_KEYS = (
    "name",
    "topic",
    "purpose",
    "is_archived",
    "is_private",
    "is_shared",
    "members",
)
_EMOJI_KEYS = ("url",)
_IMAGE_KEYS = ("url", "content_type", "size")
_FILE_EXCLUDED = (
    "shares",
    "channels",
    "groups",
    "ims",
    "has_more_shares",
    "favorites",
    "is_starred",
    "file_access",
    "comments_count",
)


def _pick(obj: dict, keys: tuple[str, ...]) -> dict:
    return {k: obj[k] for k in keys if obj.get(k) is not None}


def _message_subset(obj: dict) -> dict:
    subset = _pick(obj, _MESSAGE_KEYS)
    if obj.get("files") is not None:
        subset["files"] = [{"id": f["id"], "mode": f.get("mode")} for f in obj["files"]]
    return subset


def _file_subset(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _FILE_EXCLUDED}


_SUBSETS: dict[str, Callable[[dict], dict]] = {
    "message": _message_subset,
    "user": lambda obj: _pick(obj, _USER_KEYS),
    "emoji": lambda obj: _pick(obj, _EMOJI_KEYS),
    "conversation": lambda obj: _pick(obj, _CONVERSATION_KEYS),
    "bot": dict,
    "usergroup": dict,
    "file": _file_subset,
    "image": lambda obj: _pick(obj, _IMAGE_KEYS),
}


def content_hash(kind: Kind, obj: dict) -> str:
    """Keys that are missing or null in the hashed subset are left out, so both hash alike."""
    if kind not in _SUBSETS:
        raise ValueError(f"{kind} lines carry no content hash")
    canonical = json.dumps(
        _SUBSETS[kind](obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
