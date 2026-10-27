# ABOUTME: DirectoryView answers name lookups, falling back to the raw id for an unknown object.
# ABOUTME: Also names a line's image, a conversation's kind, and the users a message references.
import re
from dataclasses import dataclass
from typing import Literal

from slarc.domain.envelope import Kind
from slarc.domain.links import is_web_url

USER_ID = re.compile(r"[UW][A-Z0-9]{2,}")
MENTION = re.compile(r"<@([UW][A-Z0-9]{2,})[|>]")
USER_KEYS = ("user", "user_id", "author_id", "parent_user_id", "inviter")
USER_LIST_KEYS = ("users", "reply_users")


def referenced_users(obj: object) -> set[str]:
    """Every user id the object names, at any depth: in a user field, a list of users, or a
    mention in text."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in USER_KEYS and isinstance(value, str) and USER_ID.fullmatch(value):
                found.add(value)
            elif key in USER_LIST_KEYS and isinstance(value, list):
                found.update(v for v in value if isinstance(v, str) and USER_ID.fullmatch(v))
            else:
                found |= referenced_users(value)
    elif isinstance(obj, list):
        for value in obj:
            found |= referenced_users(value)
    elif isinstance(obj, str):
        found.update(MENTION.findall(obj))
    return found


def directory_image_url(kind: Kind, obj: dict) -> str | None:
    """The image URL a user (its avatar) or emoji line's blob comes from, if it has one."""
    if kind == "user":
        url = (obj.get("profile") or {}).get("image_512")
    elif kind == "emoji":
        url = obj.get("url")
    else:
        return None
    return url if is_web_url(url) else None


type ConversationKind = Literal["channel", "dm", "group_dm"]


def conversation_kind(conversation: dict) -> ConversationKind:
    if conversation.get("is_im"):
        return "dm"
    if conversation.get("is_mpim"):
        return "group_dm"
    return "channel"


@dataclass(frozen=True)
class DirectoryView:
    users: dict[str, dict]
    conversations: dict[str, dict]
    usergroups: dict[str, dict]
    emoji: dict[str, str]
    bots: dict[str, dict]

    def user_display(self, user_id: str) -> str:
        user = self.users.get(user_id)
        if user is None:
            return user_id
        profile = user.get("profile") or {}
        candidates = (
            profile.get("display_name"),
            profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        )
        return next((name for name in candidates if name), user_id)

    def conversation_name(self, channel_id: str) -> str:
        conversation = self.conversations.get(channel_id)
        if conversation is None:
            return channel_id
        if conversation.get("name"):
            return conversation["name"]
        if conversation.get("is_im") and conversation.get("user"):
            return self.user_display(conversation["user"])
        return channel_id

    def conversation_label(self, channel_id: str) -> str:
        """#name for a channel, @user for a DM, a group DM's name, or the raw id when unknown."""
        conversation = self.conversations.get(channel_id)
        if conversation is None:
            return channel_id
        kind = conversation_kind(conversation)
        if kind == "dm" and conversation.get("user"):
            return "@" + self.user_display(conversation["user"])
        if kind == "channel" and conversation.get("name"):
            return "#" + conversation["name"]
        return conversation.get("name") or channel_id

    def usergroup_handle(self, usergroup_id: str) -> str:
        return (self.usergroups.get(usergroup_id) or {}).get("handle") or usergroup_id

    def author_real_name(self, message: dict) -> str:
        """The author's real name, else author_display."""
        user = self.users.get(message.get("user") or "") or {}
        profile = user.get("profile") or {}
        return profile.get("real_name") or user.get("real_name") or self.author_display(message)

    def author_display(self, message: dict) -> str:
        if message.get("user"):
            return self.user_display(message["user"])
        return (
            (message.get("bot_profile") or {}).get("name") or message.get("username") or "unknown"
        )
