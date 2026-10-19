# ABOUTME: Pure HTML renderers for message bodies: rich_text and layout blocks, and mrkdwn text.
# ABOUTME: AssetResolver says where the rendered page finds a custom emoji, image or file.
from typing import Protocol


class AssetResolver(Protocol):
    """The src for an archived asset in the rendered output, or None when there is no copy."""

    def emoji_src(self, name: str) -> str | None: ...

    def image_src(self, url: str) -> str | None: ...

    def file_src(self, file_id: str) -> str | None: ...
