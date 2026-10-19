# ABOUTME: Tests for resolve_emoji: standard emoji with skin tones, custom images and aliases.
# ABOUTME: Unknown names and custom emoji without a local image render as :name: text.
import pytest

from slarc.domain.directory import DirectoryView
from slarc.domain.emoji import EmojiRender, code_points_text, resolve_emoji, standard_emoji

SMILE = chr(0x1F604)
WAVE_TONE_3 = chr(0x1F44B) + chr(0x1F3FC)
WHITE_LARGE_SQUARE = chr(0x2B1C)


def _view(emoji: dict[str, str]) -> DirectoryView:
    return DirectoryView(users={}, conversations={}, usergroups={}, emoji=emoji, bots={})


def _local(name: str) -> str | None:
    return f"assets/emoji/{name}"


def _missing(name: str) -> str | None:
    return None


def test_standard_emoji_renders_unicode():
    assert resolve_emoji("smile", _view({}), _local) == EmojiRender("unicode", SMILE)


def test_skin_tone_applied():
    assert resolve_emoji("wave::skin-tone-3", _view({}), _local) == EmojiRender(
        "unicode", WAVE_TONE_3
    )


def test_skin_tone_on_emoji_without_skins_keeps_base():
    assert resolve_emoji("smile::skin-tone-3", _view({}), _local).value == SMILE


def test_custom_alias_to_standard():
    view = _view({"white_square": "alias:white_large_square"})
    assert resolve_emoji("white_square", view, _local) == EmojiRender("unicode", WHITE_LARGE_SQUARE)


def test_custom_emoji_renders_local_image():
    view = _view({"teamwork": "https://emoji.slack-edge.com/T/teamwork/a.gif"})
    assert resolve_emoji("teamwork", view, _local) == EmojiRender("image", "assets/emoji/teamwork")


def test_custom_alias_chain_resolves_to_target_image():
    view = _view({"teamwork": "https://e.test/t.gif", "tw2": "alias:teamwork", "tw3": "alias:tw2"})
    assert resolve_emoji("tw3", view, _local) == EmojiRender("image", "assets/emoji/teamwork")


def test_custom_emoji_without_local_image_renders_text():
    view = _view({"teamwork": "https://e.test/t.gif"})
    assert resolve_emoji("teamwork", view, _missing) == EmojiRender("text", ":teamwork:")


def test_alias_cycle_renders_text():
    view = _view({"a": "alias:b", "b": "alias:a"})
    assert resolve_emoji("a", view, _local) == EmojiRender("text", ":a:")


def test_unknown_emoji_renders_colon_name():
    assert resolve_emoji("no_such_emoji", _view({}), _local) == EmojiRender(
        "text", ":no_such_emoji:"
    )


def test_table_keys_every_short_name():
    table = standard_emoji()
    assert table["+1"] == table["thumbsup"]
    assert table["smile"] == {"u": "1F604"}
    assert table["wave"]["skins"]["3"] == "1F44B-1F3FC"


def test_code_points_text_rejects_surrogates():
    assert code_points_text("1F44B-1F3FC") == WAVE_TONE_3
    with pytest.raises(ValueError):
        code_points_text("D800")
