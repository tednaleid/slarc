# ABOUTME: resolve_emoji turns a Slack emoji name into unicode, a custom image or :name: text.
# ABOUTME: Applies ::skin-tone-N, follows custom aliases and falls back to the standard table.
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Literal

from slarc.domain.directory import DirectoryView

ALIAS_PREFIX = "alias:"
SKIN_TONE_PREFIX = "skin-tone-"
MAX_ALIAS_DEPTH = 10


@dataclass(frozen=True)
class EmojiRender:
    kind: Literal["unicode", "image", "text"]
    value: str


@cache
def standard_emoji() -> dict[str, dict]:
    """The packaged table generated from iamcal/emoji-data: short name to code points."""
    return json.loads(files("slarc").joinpath("data/emoji.json").read_text(encoding="utf-8"))


def resolve_emoji(
    name: str, view: DirectoryView, image_url_for: Callable[[str], str | None]
) -> EmojiRender:
    """How to show name: custom emoji (through aliases) as image_url_for's local image, standard
    emoji as unicode with any skin tone applied, and anything else as :name: text."""
    base, _, tones = name.partition("::")
    target = base
    for _ in range(MAX_ALIAS_DEPTH):
        url = view.emoji.get(target)
        if url is None:
            break
        if not url.startswith(ALIAS_PREFIX):
            src = image_url_for(target)
            return EmojiRender("image", src) if src else EmojiRender("text", f":{name}:")
        target = url.removeprefix(ALIAS_PREFIX)
    else:
        return EmojiRender("text", f":{name}:")
    code = _standard_code(target, _skin_tone_key(tones))
    if code is None:
        return EmojiRender("text", f":{name}:")
    return EmojiRender("unicode", code_points_text(code))


def code_points_text(code: str) -> str:
    """The text for dash-separated hex code points such as 1F44B-1F3FC."""
    return "".join(chr(int(point, 16)) for point in code.split("-"))


def _skin_tone_key(tones: str) -> str:
    """The table's skin key for the part after the base name: skin-tone-3 gives 3, and
    skin-tone-2::skin-tone-4 or skin-tone-2-4 gives 2-4."""
    parts = [part.removeprefix(SKIN_TONE_PREFIX) for part in tones.split("::") if part]
    return "-".join(parts)


def _standard_code(name: str, skin: str) -> str | None:
    entry = standard_emoji().get(name)
    if entry is None:
        return None
    skins = entry.get("skins") or {}
    if skin:
        return skins.get(skin) or skins.get(f"{skin}-{skin}") or entry["u"]
    return entry["u"]
