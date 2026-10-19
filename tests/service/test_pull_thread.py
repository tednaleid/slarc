# ABOUTME: Tests for pulling tracked threads and single messages, including the directory refresh.
# ABOUTME: A fake gateway serves the spike fixtures; edits and deletes change its data.
import json

import pytest

from slarc import __version__
from slarc.domain.envelope import Envelope, Meta
from slarc.domain.hashing import content_hash
from slarc.domain.links import ConversationSel
from slarc.domain.tracking import DownloadState
from slarc.ports import SlarcError
from slarc.service.selectors import resolve_conversation
from tests.recorded_fixtures import recorded_directory
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    PARENT_TS,
    REPLY_TS,
    TEAM,
    THREAD_TS,
    Harness,
    spike,
)

LONE_TS = "1792422552.941749"
JIM = "U0CA61XJF2R"
THREAD_LINK = f"{BASE}/p1792410693663869"
REPLY_LINK = f"{BASE}/p1792410743787639?thread_ts={PARENT_TS}&cid={CHANNEL}"
LONE_LINK = f"{BASE}/p1792422552941749"


@pytest.fixture
def h() -> Harness:
    return Harness()


def test_thread_link_appends_parent_and_replies(h: Harness):
    report = h.pull(THREAD_LINK)

    messages = h.lines("message")
    assert [env.meta.ts for env in messages] == THREAD_TS
    parent = messages[0]
    assert parent.object is not None
    assert parent.object == spike("conversations.replies")["messages"][0]
    assert parent.meta == Meta(
        kind="message",
        team=TEAM,
        channel=CHANNEL,
        ts=PARENT_TS,
        source_team=TEAM,
        fetched_at="2026-10-19T13:40:11Z",
        run="r_20261019T134011Z",
        api="conversations.replies",
        slarc=__version__,
        hash=content_hash("message", parent.object),
        plain="jim: Hello!",
    )
    assert messages[3].meta.source_team == TEAM
    assert report.messages_seen == 4
    assert report.lines_appended == len(h.archive.lines) - 1
    assert report.skipped == []
    [item] = h.tracked()
    assert (item.team_id, item.kind, item.channel, item.ts) == (TEAM, "thread", CHANNEL, PARENT_TS)
    assert item.status == "ok"
    assert item.last_pull_at == "2026-10-19T13:40:11Z"


def test_second_pull_appends_nothing(h: Harness):
    h.pull(THREAD_LINK)
    before = list(h.archive.lines)

    report = h.pull()

    assert h.archive.lines[: len(before)] == before
    assert [env.meta.kind for env in h.archive.lines[len(before) :]] == ["run"]
    assert report.lines_appended == 0
    assert report.messages_seen == 4
    assert [c for c in h.gateway.calls if c[0] == "bots.info"] == [("bots.info", "B0CA9EBAH1C")]


def test_edit_appends_one_line(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    h.change_message(REPLY_TS, text="edited text", edited={"user": ME, "ts": "1792500000.000000"})

    report = h.pull()

    [new] = h.archive.lines[before:-1]
    assert (new.meta.kind, new.meta.ts) == ("message", REPLY_TS)
    assert new.object is not None
    assert new.object["text"] == "edited text"
    assert report.lines_appended == 1


def test_reaction_change_appends_one_line(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    h.change_message(PARENT_TS, reactions=[{"name": "tada", "users": [ME], "count": 1}])

    h.pull()

    [new] = h.archive.lines[before:-1]
    assert (new.meta.kind, new.meta.ts) == ("message", PARENT_TS)


def test_removed_reply_gets_deleted_line(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    h.remove_messages(REPLY_TS)

    h.pull()

    [deleted] = h.archive.lines[before:-1]
    assert deleted == Envelope(
        meta=Meta(
            kind="deleted",
            team=TEAM,
            channel=CHANNEL,
            ts=REPLY_TS,
            fetched_at="2026-10-19T14:40:11Z",
            run="r_20261019T144011Z",
            slarc=__version__,
        )
    )
    assert h.archive.stored_messages(TEAM, CHANNEL)[REPLY_TS].hash is None


def test_lone_message_tracked_as_message(h: Harness):
    h.pull(LONE_LINK)

    assert [env.meta.ts for env in h.lines("message")] == [LONE_TS]
    [item] = h.tracked()
    assert (item.kind, item.channel, item.ts) == ("message", CHANNEL, LONE_TS)


def test_reply_link_tracks_whole_thread(h: Harness):
    h.pull(REPLY_LINK)

    assert [env.meta.ts for env in h.lines("message")] == THREAD_TS
    [item] = h.tracked()
    assert (item.kind, item.ts) == ("thread", PARENT_TS)


def test_directory_written_before_messages(h: Harness):
    h.pull(THREAD_LINK)

    kinds = [env.meta.kind for env in h.archive.lines]
    first_message = kinds.index("message")
    assert {"user", "emoji", "conversation", "bot"} <= set(kinds[:first_message])
    assert not {"user", "emoji", "conversation", "bot", "usergroup"} & set(kinds[first_message:])

    users = h.lines("user")
    assert {env.meta.id for env in users} == {"USLACKBOT", ME, "U0CA61XJF2R"}
    assert {env.meta.api for env in users} == {"users.list"}
    emoji = {env.meta.id: env for env in h.lines("emoji")}
    assert emoji["shipit"].object == {"name": "shipit", "url": "alias:squirrel"}
    assert emoji["shipit"].meta.api == "emoji.list"
    assert emoji["shipit"].meta.hash == content_hash("emoji", {"url": "alias:squirrel"})
    [conversation] = h.lines("conversation")
    assert (conversation.meta.id, conversation.meta.api) == (CHANNEL, "conversations.info")
    assert conversation.object == spike("conversations.info")["channel"]
    [bot] = h.lines("bot")
    assert (bot.meta.id, bot.meta.api, bot.meta.team) == ("B0CA9EBAH1C", "bots.info", TEAM)


def test_rename_appends_one_user_line(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.lines("user"))
    h.gateway.users_list = list(recorded_directory("users.jsonl").values())

    h.pull()

    renamed = [env for env in h.lines("user")[before:] if env.meta.id == JIM]
    assert len(renamed) == 1
    assert renamed[0].object is not None
    assert renamed[0].object["profile"]["display_name"] == "Jim"


def test_file_lines_written_for_referenced_files(h: Harness):
    report = h.pull(THREAD_LINK)

    inline = spike("conversations.replies")["messages"][3]["files"][0]
    [file_line] = h.lines("file")
    assert file_line.object == inline
    assert file_line.meta.id == "F0CB7HWPUTA"
    assert file_line.meta.api == "conversations.replies"
    assert file_line.meta.hash == content_hash("file", inline)
    assert [d for d in h.last_run().downloads if d.target_kind == "file"] == [
        DownloadState(
            team_id=TEAM,
            target_kind="file",
            target="F0CB7HWPUTA",
            url=inline["url_private_download"],
            status="pending",
        )
    ]
    assert report.downloads_pending == len(h.last_run().downloads)

    h.pull()
    assert len(h.lines("file")) == 1


def test_downloaded_file_keeps_its_blob_and_is_not_queued_again(h: Harness):
    h.pull(THREAD_LINK)
    info = spike("files.info.F0CB7HWPUTA")["file"]
    blob, _ = h.blobs.write([b"png bytes"])
    h.archive.append(
        Envelope(
            meta=Meta(
                kind="file",
                team=TEAM,
                id="F0CB7HWPUTA",
                fetched_at="2026-10-19T14:00:00Z",
                run="r_20261019T140000Z",
                api="files.info",
                hash=content_hash("file", info),
                blob=blob,
            ),
            object=info,
        )
    )

    h.pull()

    assert {env.meta.blob for env in h.lines("file")[1:]} == {blob}
    assert [d for d in h.last_run().downloads if d.target_kind == "file"] == []


def test_last_read_stored_per_user(h: Harness):
    h.pull(THREAD_LINK)

    [item] = h.tracked()
    assert item.last_read == {ME: "1792422623.464629"}


def test_thread_not_found_skips_without_deletes(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    h.remove_messages(*THREAD_TS)

    report = h.pull()

    assert [env.meta.kind for env in h.archive.lines[before:]] == ["run"]
    [item] = h.tracked()
    assert item.status == "not_visible"
    assert h.reporter.warnings == [f"Slack returns no message at {THREAD_LINK}; skipping it."]
    assert report.skipped == h.reporter.warnings


def test_missing_message_link_is_not_tracked(h: Harness):
    report = h.pull(f"{BASE}/p1792499999000001")

    assert h.lines("message") == []
    assert h.tracked() == []
    assert h.reporter.warnings == [
        f"Slack returns no message at {BASE}/p1792499999000001; skipping it."
    ]
    assert report.skipped == h.reporter.warnings


def test_deletes_count_only_from_the_oldest_returned_message(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    served = h.gateway.replies

    def without_oldest_two(channel: str, ts: str) -> list[dict]:
        return served(channel, ts)[2:]

    monkeypatch.setattr(h.gateway, "replies", without_oldest_two)

    h.pull()

    assert h.lines("deleted") == []
    assert [env.meta.kind for env in h.archive.lines[before:]] == ["run"]


def test_run_records_auth_per_workspace(h: Harness):
    h.pull(THREAD_LINK, LONE_LINK)

    run = h.last_run()
    assert run.teams == [TEAM]
    assert run.auth == [
        {
            "team_id": TEAM,
            "user_id": ME,
            "domain": "naleidworkspace",
            "credential_source": "desktop",
            "auth_test_at": "2026-10-19T13:40:11Z",
        }
    ]
    run_line = json.dumps(h.lines("run")[-1].object)
    assert "xoxc-1" not in run_line
    assert "xoxd-1" not in run_line


def test_not_visible_item_is_skipped_with_warning(h: Harness):
    h.pull(THREAD_LINK)
    before = len(h.archive.lines)
    del h.gateway.conversations[CHANNEL]

    report = h.pull()

    assert [env.meta.kind for env in h.archive.lines[before:]] == ["run"]
    [item] = h.tracked()
    assert item.status == "not_visible"
    assert h.reporter.warnings == [f"Conversation {CHANNEL} is not visible to this account."]
    assert report.skipped == h.reporter.warnings


def test_connects_once_per_workspace(h: Harness):
    h.pull(THREAD_LINK, LONE_LINK)

    assert h.connector.connects == ["naleidworkspace"]
    assert {item.kind for item in h.tracked()} == {"thread", "message"}


def test_non_link_selector_needs_a_single_workspace(h: Harness):
    with pytest.raises(SlarcError, match="link"):
        h.pull("#all-naleid")


def test_channel_name_resolves_through_stored_conversations(h: Harness):
    h.pull(THREAD_LINK)
    h.gateway.calls.clear()
    sel = ConversationSel(domain=None, channel=None, name="all-naleid", dm_user=None)

    assert resolve_conversation(sel, TEAM, h.gateway, h.archive) == CHANNEL
    assert h.gateway.calls == []


def test_channel_name_falls_back_to_listed_conversations(h: Harness):
    h.gateway.infos = {c["id"]: c for c in spike("conversations.list")["channels"]}
    h.gateway.conversations = {c: [] for c in h.gateway.infos}
    sel = ConversationSel(domain=None, channel=None, name="new-channel", dm_user=None)

    assert resolve_conversation(sel, TEAM, h.gateway, h.archive) == "C0CA53DLVJP"


def test_dm_user_resolves_to_their_dm(h: Harness):
    sel = ConversationSel(domain=None, channel=None, name=None, dm_user="jim")
    h.gateway.infos = {c["id"]: c for c in spike("conversations.list")["channels"]}
    h.gateway.conversations = {c: [] for c in h.gateway.infos}

    assert resolve_conversation(sel, TEAM, h.gateway, h.archive) == "D0CAD7JE1QR"


def test_progress_and_item_count_reported_per_thread(h: Harness):
    report = h.pull(THREAD_LINK, LONE_LINK)

    assert h.reporter.progress_updates == [
        (f"#all-naleid thread {PARENT_TS}", 4, 4),
        (f"#all-naleid message {LONE_TS}", 1, 1),
    ]
    assert report.items_pulled == 2


def test_skipped_item_is_not_counted_as_pulled(h: Harness):
    h.pull(THREAD_LINK)
    del h.gateway.conversations[CHANNEL]

    report = h.pull()

    assert report.items_pulled == 0
    assert h.reporter.progress_updates[1:] == []
