# ABOUTME: Pure HTML and markdown renderers for message bodies: blocks and mrkdwn text.
# ABOUTME: AssetResolver says where a rendered page finds an asset; the helpers read Slack fields.
from typing import Protocol


class AssetResolver(Protocol):
    """The src for an archived asset in the rendered output, or None when there is no copy."""

    def emoji_src(self, name: str) -> str | None: ...

    def image_src(self, url: str) -> str | None: ...

    def file_src(self, file_id: str) -> str | None: ...


def text_field(value: object) -> str:
    """value when it is a string, else "", so malformed fields render as nothing."""
    return value if isinstance(value, str) else ""


def dict_list(value: object) -> list[dict]:
    """The dicts of a list field, or none when the field is not a list."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def fallback_text(obj: dict) -> str:
    """The text of an element or block of a type the renderers do not know."""
    text = obj.get("text")
    if isinstance(text, dict):
        text = text.get("text")
    for candidate in (text, obj.get("fallback"), obj.get("url")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""
