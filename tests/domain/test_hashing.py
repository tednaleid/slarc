# ABOUTME: Tests for the content hash of each envelope kind's hashed subset.
# ABOUTME: Covers fields excluded from the hash, fields that change it and key order independence.
import copy
import json
from pathlib import Path

import pytest

from slarc.domain.hashing import content_hash

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"


def thread_parent() -> dict:
    return json.loads((SPIKE / "conversations.replies.json").read_text())["messages"][0]


def test_hash_is_sha256_hex():
    assert len(content_hash("message", thread_parent())) == 64


def test_hash_ignores_reply_count_latest_reply_last_read_subscribed():
    parent = thread_parent()
    before = content_hash("message", parent)
    changed = copy.deepcopy(parent)
    changed["reply_count"] = 99
    changed["reply_users"] = ["U1", "U2"]
    changed["reply_users_count"] = 2
    changed["latest_reply"] = "1799999999.000000"
    changed["last_read"] = "1799999999.000000"
    changed["subscribed"] = False
    changed["is_locked"] = True
    assert content_hash("message", changed) == before


def test_hash_changes_on_edit_reaction_and_attachment_removal():
    parent = thread_parent()
    before = content_hash("message", parent)

    edited = copy.deepcopy(parent)
    edited["text"] = "Hello, edited"
    edited["edited"] = {"user": "U0CA61XJF2R", "ts": "1792410700.000000"}
    assert content_hash("message", edited) != before

    reacted = copy.deepcopy(parent)
    reacted["reactions"].append({"name": "tada", "users": ["U1"], "count": 1})
    assert content_hash("message", reacted) != before

    with_attachment = copy.deepcopy(parent)
    with_attachment["attachments"] = [{"id": 1, "text": "a"}]
    with_files = copy.deepcopy(parent)
    with_files["files"] = [{"id": "F1", "mode": "hosted"}]
    assert content_hash("message", with_attachment) != before
    assert content_hash("message", with_files) != before

    removed = copy.deepcopy(with_attachment)
    del removed["attachments"]
    assert content_hash("message", removed) == before


def test_hash_independent_of_key_order():
    parent = thread_parent()
    reordered = dict(reversed(list(parent.items())))
    assert list(reordered) != list(parent)
    assert content_hash("message", reordered) == content_hash("message", parent)


def test_message_hash_treats_null_and_missing_alike():
    parent = thread_parent()
    with_null = {**parent, "edited": None}
    assert content_hash("message", with_null) == content_hash("message", parent)


def test_message_files_hash_id_and_mode_only():
    parent = thread_parent()
    a = {**parent, "files": [{"id": "F1", "mode": "hosted", "size": 1, "shares": {"x": 1}}]}
    b = {**parent, "files": [{"id": "F1", "mode": "hosted", "size": 2}]}
    tombstone = {**parent, "files": [{"id": "F1", "mode": "tombstone"}]}
    assert content_hash("message", a) == content_hash("message", b)
    assert content_hash("message", a) != content_hash("message", tombstone)


def test_message_hash_ignores_fields_outside_subset():
    parent = thread_parent()
    other = {**parent, "client_msg_id": "other", "team": "T9", "thread_ts": "1.0"}
    assert content_hash("message", other) == content_hash("message", parent)


def blocks_message(block_id: str, text: str = "hello") -> dict:
    section = {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}
    block = {"type": "rich_text", "block_id": block_id, "elements": [section]}
    attachment = {"id": 1, "blocks": [{"type": "section", "block_id": f"{block_id}-a"}]}
    return {"type": "message", "text": text, "blocks": [block], "attachments": [attachment]}


def test_message_hash_ignores_block_ids():
    assert content_hash("message", blocks_message("WK/f")) == content_hash(
        "message", blocks_message("CQXr")
    )


def test_message_hash_changes_on_text_inside_a_block():
    before = content_hash("message", blocks_message("WK/f"))
    changed = blocks_message("WK/f")
    changed["blocks"][0]["elements"][0]["elements"][0]["text"] = "goodbye"
    assert content_hash("message", changed) != before


def test_message_hash_leaves_the_message_unchanged():
    message = blocks_message("WK/f")
    content_hash("message", message)
    assert message == blocks_message("WK/f")


def file_object() -> dict:
    return json.loads((SPIKE / "files.info.F0C9XQFTVRD.json").read_text())["file"]


def test_file_hash_ignores_shares():
    f = file_object()
    before = content_hash("file", f)
    changed = copy.deepcopy(f)
    changed["shares"] = {"public": {"C1": [{"ts": "1.0"}]}}
    changed["channels"] = ["C1", "C2"]
    changed["is_starred"] = True
    assert content_hash("file", changed) == before


def test_inline_and_files_info_objects_hash_alike():
    replies = json.loads((SPIKE / "conversations.replies.json").read_text())["messages"]
    [inline] = [f for m in replies for f in m.get("files") or [] if f["id"] == "F0CB7HWPUTA"]
    info = json.loads((SPIKE / "files.info.F0CB7HWPUTA.json").read_text())["file"]
    assert "skipped_shares" in inline
    assert content_hash("file", inline) == content_hash("file", info)


def test_file_hash_changes_on_title():
    f = file_object()
    changed = {**f, "title": "renamed"}
    assert content_hash("file", changed) != content_hash("file", f)


def users() -> list[dict]:
    return json.loads((SPIKE / "users.list.json").read_text())["members"]


def test_user_hash_changes_with_profile_not_with_updated():
    user = users()[0]
    before = content_hash("user", user)
    renamed = copy.deepcopy(user)
    renamed["profile"]["real_name"] = "Someone Else"
    assert content_hash("user", renamed) != before
    touched = {**user, "updated": 1}
    assert content_hash("user", touched) == before


def test_image_hash_covers_url_type_and_size():
    img = {"url": "https://x/y.png", "content_type": "image/png", "size": 10}
    assert content_hash("image", img) != content_hash("image", {**img, "size": 11})
    assert content_hash("image", img) != content_hash("image", {**img, "content_type": "x"})


def test_emoji_hash_covers_url_only():
    e = {"name": "party", "url": "https://x/p.png"}
    assert content_hash("emoji", e) == content_hash("emoji", {**e, "name": "other"})
    assert content_hash("emoji", e) != content_hash("emoji", {**e, "url": "alias:wave"})


def test_conversation_hash_ignores_last_read():
    conv = {"id": "C1", "name": "general", "topic": {"value": "t"}, "is_archived": False}
    before = content_hash("conversation", conv)
    assert content_hash("conversation", {**conv, "last_read": "1.0"}) == before
    assert content_hash("conversation", {**conv, "is_archived": True}) != before


def test_bot_and_usergroup_hash_whole_object():
    bot = {"id": "B1", "name": "bot"}
    assert content_hash("bot", bot) != content_hash("bot", {**bot, "name": "x"})
    group = {"id": "S1", "handle": "g"}
    assert content_hash("usergroup", group) != content_hash("usergroup", {**group, "handle": "h"})


@pytest.mark.parametrize("kind", ["deleted", "run"])
def test_kinds_without_a_hash_are_rejected(kind):
    with pytest.raises(ValueError):
        content_hash(kind, {})
