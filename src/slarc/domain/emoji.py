# ABOUTME: resolve_emoji turns a Slack emoji name into unicode, a custom image or :name: text,
# ABOUTME: through custom aliases and skin tones; also finds :name: shortcodes in mrkdwn text.
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Literal

from slarc.domain.directory import DirectoryView

ALIAS_PREFIX = "alias:"
SKIN_TONE_PREFIX = "skin-tone-"
MAX_ALIAS_DEPTH = 10
SKIN_TONE_CODES = {"1F3FB": "2", "1F3FC": "3", "1F3FD": "4", "1F3FE": "5", "1F3FF": "6"}
SURROGATES = (0xD800, 0xDFFF)
# The name inside a :name: shortcode in mrkdwn, with any skin tones.
SHORTCODE = r"[a-z0-9_+'-]+(?:::skin-tone-[2-6](?:-[2-6])?)*"
_SHORTCODE_IN_TEXT = re.compile(rf":({SHORTCODE}):")


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
    target = _alias_target(base, view.emoji)
    if target is None:
        return EmojiRender("text", f":{name}:")
    if target in view.emoji:
        src = image_url_for(target)
        return EmojiRender("image", src) if src else EmojiRender("text", f":{name}:")
    code = _standard_code(target, _skin_key_from_name(tones))
    if code is None:
        return EmojiRender("text", f":{name}:")
    return EmojiRender("unicode", code_points_text(code))


def custom_emoji_url(name: str, emoji: dict[str, str]) -> str | None:
    """The image URL of a custom emoji, through its aliases; None for any other name."""
    target = _alias_target(name, emoji)
    return emoji.get(target) if target is not None else None


def shortcodes(text: str) -> list[str]:
    """The emoji names in the :name: shortcodes of mrkdwn text, skin tones included."""
    return _SHORTCODE_IN_TEXT.findall(text)


def _alias_target(name: str, emoji: dict[str, str]) -> str | None:
    """The name name's custom aliases lead to: a custom emoji with an image, or a name that is
    not custom. None for an alias cycle or a chain longer than MAX_ALIAS_DEPTH."""
    target = name
    for _ in range(MAX_ALIAS_DEPTH):
        url = emoji.get(target)
        if url is None or not url.startswith(ALIAS_PREFIX):
            return target
        target = url.removeprefix(ALIAS_PREFIX)
    return None


def code_points_text(code: str) -> str:
    """The text for dash-separated hex code points such as 1F44B-1F3FC. Raises ValueError for
    anything that is not a list of valid, non-surrogate code points."""
    points = [int(point, 16) for point in code.split("-")]
    if any(SURROGATES[0] <= point <= SURROGATES[1] for point in points):
        raise ValueError(f"surrogate code point in {code}")
    return "".join(chr(point) for point in points)


def skin_key_from_code(code: str) -> str:
    """The skin key for the tone modifiers in code points such as 1f44b-1f3fc (3), or ""."""
    tones = [
        SKIN_TONE_CODES[point] for point in code.upper().split("-") if point in SKIN_TONE_CODES
    ]
    return "-".join(tones)


def _skin_key_from_name(tones: str) -> str:
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
