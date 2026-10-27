# ABOUTME: Tests for apply_batch, which writes the changes RTM events ask for into a listen unit of
# ABOUTME: work: stored messages, deletes, quiet re-reads, user lines and the batch's downloads.
from datetime import timedelta

import msgspec
import pytest

from slarc import __version__
from slarc.domain.events import Change, DeleteMessage, Reread, StoreMessage, UserChanged
from slarc.ports import Commit, SlarcError, StoredMessage
from slarc.service.listen import ListenOptions, Pending, apply_batch
from slarc.service.workspaces import Workspaces
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import BASE, CHANNEL, PARENT_TS, TEAM, Harness, message

NEW_TS = "1792500000.000100"
OPTIONS = ListenOptions(downloads=4, max_file_bytes=100 * 1024 * 1024)


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.pull(BASE)
    harness.gateway.calls.clear()
    return harness


def apply(h: Harness, changes: list[Change], pending: Pending | None = None) -> list[str]:
    pending = pending if pending is not None else Pending()
    uow = UnitOfWork(h.archive, h.blobs, h.clock, "listen", [], __version__)
    with uow:
        workspaces = Workspaces(h.connector, uow)
        return apply_batch(
            uow, workspaces, TEAM, changes, pending, h.downloader, OPTIONS, h.reporter
        )


def lines_at(h: Harness, ts: str) -> list[str]:
    return [env.meta.kind for env in h.archive.lines if env.meta.ts == ts]


def test_new_message_on_a_tracked_conversation_is_stored_from_the_event(h: Harness):
    said = apply(h, [StoreMessage(CHANNEL, message(NEW_TS, text="hello"))])

    assert not [call for call in h.gateway.calls if str(call[0]).startswith("conversations.")]
    [line] = [env for env in h.archive.lines if env.meta.ts == NEW_TS]
    assert (line.meta.kind, line.meta.api) == ("message", "rtm")
    assert said == ["#all-naleid  new message from Ted Naleid"]


def test_an_edit_says_edited(h: Harness):
    said = apply(h, [StoreMessage(CHANNEL, message(PARENT_TS, text="changed", edited={"ts": "1"}))])

    assert said == ["#all-naleid  edited message from Ted Naleid"]


def test_event_for_an_untracked_channel_is_dropped(h: Harness):
    said = apply(h, [StoreMessage("C0UNTRACKED", message(NEW_TS))])

    assert said == []
    assert lines_at(h, NEW_TS) == []


def test_delete_appends_a_deleted_version(h: Harness):
    said = apply(h, [DeleteMessage(CHANNEL, PARENT_TS)])

    assert lines_at(h, PARENT_TS)[-1] == "deleted"
    assert said == [f"#all-naleid  deleted message {PARENT_TS}"]


def test_a_delete_of_a_message_already_deleted_writes_nothing(h: Harness):
    apply(h, [DeleteMessage(CHANNEL, PARENT_TS)])
    before = len(h.archive.lines)

    assert apply(h, [DeleteMessage(CHANNEL, PARENT_TS)]) == []
    assert [env.meta.kind for env in h.archive.lines[before:]] == ["run"]


def test_many_reactions_on_a_thread_reread_it_once(h: Harness):
    pending = Pending()
    apply(h, [Reread(CHANNEL, PARENT_TS)] * 50, pending)
    h.clock.advance(5)
    apply(h, [Reread(CHANNEL, PARENT_TS)] * 50, pending)
    assert h.gateway.calls == []

    h.clock.advance(11)
    said = apply(h, [], pending)

    assert h.gateway.calls == [("conversations.replies", CHANNEL, PARENT_TS)]
    assert said == [f"#all-naleid  re-read thread {PARENT_TS}"]


def test_batch_for_an_item_removed_meanwhile_writes_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    stored = h.archive.stored_messages

    def rm_commits_first(team: str, channel: str) -> dict[str, StoredMessage]:
        with h.archive.elsewhere() as archive:
            [item] = archive.tracked_items().values()
            archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))
        return stored(team, channel)

    monkeypatch.setattr(h.archive, "stored_messages", rm_commits_first)
    pending = Pending()

    said = apply(h, [StoreMessage(CHANNEL, message(NEW_TS))], pending)

    assert lines_at(h, NEW_TS) == []
    assert pending.catch_up == {TEAM}
    assert said == ["#all-naleid  changed meanwhile; pulled next"]


def test_batch_downloads_only_its_own_targets(h: Harness):
    pending_before = [s for s in h.download_states() if s.status == "pending"]
    assert pending_before
    jobs_before = len(h.downloader.jobs)

    # No author, so the message names none of the avatars and files still pending.
    apply(h, [StoreMessage(CHANNEL, message(NEW_TS, user=None))])

    assert h.downloader.jobs[jobs_before:] == []


def test_user_change_appends_the_user(h: Harness):
    user = {"id": "U0NEWUSER1", "name": "ada", "real_name": "Ada Example", "profile": {}}

    said = apply(h, [UserChanged(user)])

    [line] = [env for env in h.lines("user") if env.meta.id == "U0NEWUSER1"]
    assert line.meta.api == "rtm"
    assert said == ["Ada Example changed their profile"]


def test_pending_rereads_wait_until_quiet() -> None:
    pending = Pending()
    start = Harness().clock.now()
    pending.mark(TEAM, CHANNEL, PARENT_TS, start)
    pending.mark(TEAM, CHANNEL, PARENT_TS, start + timedelta(seconds=8))

    assert pending.due(start + timedelta(seconds=17)) == []
    assert pending.due(start + timedelta(seconds=18)) == [(TEAM, CHANNEL, PARENT_TS)]
    assert pending.due(start + timedelta(seconds=30)) == []


def test_a_batch_that_cannot_reach_slack_queues_a_catch_up(h: Harness):
    h.gateway.auth_error = SlarcError("Could not reach Slack to call auth.test.")
    pending = Pending()

    said = apply(h, [StoreMessage(CHANNEL, message(NEW_TS))], pending)

    assert pending.catch_up == {TEAM}
    assert lines_at(h, NEW_TS) == []
    assert said == [f"{TEAM}  Could not reach Slack to call auth.test. Pulling it next."]
