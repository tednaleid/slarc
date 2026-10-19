# ABOUTME: DirectoryView answers name lookups, falling back to the raw id for an unknown object.
# ABOUTME: Also names the image whose blob a user or emoji line carries.
from dataclasses import dataclass

from slarc.domain.envelope import Kind
from slarc.domain.links import is_web_url


def directory_image_url(kind: Kind, obj: dict) -> str | None:
    """The image URL a user (its avatar) or emoji line's blob comes from, if it has one."""
    if kind == "user":
        url = (obj.get("profile") or {}).get("image_512")
    elif kind == "emoji":
        url = obj.get("url")
    else:
        return None
    return url if is_web_url(url) else None


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

    def usergroup_handle(self, usergroup_id: str) -> str:
        return (self.usergroups.get(usergroup_id) or {}).get("handle") or usergroup_id

    def author_display(self, message: dict) -> str:
        if message.get("user"):
            return self.user_display(message["user"])
        return (
            (message.get("bot_profile") or {}).get("name") or message.get("username") or "unknown"
        )
