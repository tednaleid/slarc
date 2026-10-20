# ABOUTME: Tests for the removal use case: pull spike fixtures into an in-memory archive, then rm.
# ABOUTME: Each sweep is checked to delete exactly the lines, files, images and blobs planned.
from dataclasses import dataclass

import pytest

from slarc import __version__
from slarc.domain.links import parse_selector
from slarc.domain.tracking import RunRecord, TrackedItem
from slarc.ports import SearchHit, SlarcError
from slarc.service.remove import Removal, apply_removal, plan_removal, sweep
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    OTHER,
    OTHER_CHANNEL,
    OTHER_LINK,
    OTHER_TEAM,
    PARENT_TS,
    REPLY_TS,
    SPIKE_FILES,
    TEAM,
    THREAD_TS,
    TOMBSTONE_FILE,
    Harness,
    message,
    other_gateway,
    spike,
    ts_on,
)

THREAD_LINK = f"{BASE}/p1792410693663869"
REPLY_LINK = f"{BASE}/p{REPLY_TS.replace('.', '')}?thread_ts={PARENT_TS}&cid={CHANNEL}"
SNIPPET_LINK = f"{BASE}/p1792422552941749"
REPLY_MESSAGE_LINK = f"{BASE}/p{REPLY_TS.replace('.', '')}"
SECOND_CHANNEL = "C0SECOND01"
SECOND_LINK = f"https://naleidworkspace.slack.com/archives/{SECOND_CHANNEL}"
THREAD_FILE = "F0CB7HWPUTA"
GIPHY_URL = (
    "https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif"
    "?cid=6104955ee4adf136dcbbrjmn0qdsjgxzsga2bbxafvz2c6fe&ep=v1_gifs_translate&rid=giphy.gif&ct=g"
)
CHANNEL_MESSAGES = 9


class WorkspaceIndex:
    """Answers the workspace domains query; rm reads nothing else from the index."""

    def sql(self, query: str) -> tuple[list[str], list[tuple]]:
        assert "FROM workspace" in query
        return ["team_id", "domain"], [(TEAM, "naleidworkspace"), (OTHER_TEAM, "other")]

    def search(
        self, text: str, in_channel: str | None, from_user: str | None, limit: int
    ) -> list[SearchHit]:
        raise AssertionError("rm does not search")

    def conversation_counts(self) -> dict[tuple[str, str], int]:
        raise AssertionError("rm does not count conversations")

    def messages(
        self, team: str, channel: str, since: str | None, until: str | None, include_deleted: bool
    ) -> list[dict]:
        raise AssertionError("rm does not read messages from the index")

    def thread(self, team: str, channel: str, thread_ts: str, include_deleted: bool) -> list[dict]:
        raise AssertionError("rm does not read threads from the index")


@dataclass(frozen=True)
class Inventory:
    lines: int
    files: set[str]
    images: set[str]
    blobs: set[str]


@pytest.fixture
def h() -> Harness:
    h = Harness()
    h.gateway.conversations[SECOND_CHANNEL] = [message(ts_on(18, 9))]
    h.connector.gateways[OTHER] = other_gateway()
    return h


def pull_and_download(h: Harness, *links: str) -> None:
    """Pulls the links, then pulls again with every queued download served."""
    h.pull(*links)
    for job in h.downloader.jobs:
        h.downloader.serve(job.url, job.url.encode(), "image/png")
    h.pull()


def plan(h: Harness, *texts: str, channel_mode: bool = False) -> Removal:
    selectors = [(text, parse_selector(text)) for text in texts]
    return plan_removal(selectors, channel_mode, h.archive, h.blobs, WorkspaceIndex())


def apply(h: Harness, removal: Removal, *texts: str) -> None:
    uow = UnitOfWork(h.archive, h.blobs, h.clock, "rm", list(texts), __version__)
    with uow:
        apply_removal(removal, uow)
    h.clock.advance(3600)


def inventory(h: Harness) -> Inventory:
    lines = h.archive.lines
    return Inventory(
        lines=sum(1 for env in lines if env.meta.kind in ("message", "deleted")),
        files={f"{env.meta.team}/{env.meta.id}" for env in lines if env.meta.kind == "file"},
        images={f"{env.meta.team}/{env.meta.id}" for env in lines if env.meta.kind == "image"},
        blobs=set(h.blobs.blobs),
    )


def swept(h: Harness, removal: Removal) -> None:
    """Runs the sweep and checks it deleted exactly what the removal planned."""
    before = inventory(h)
    sweep(removal.teams, h.archive, h.blobs)
    after = inventory(h)
    deleted = (
        before.lines - after.lines,
        len(before.files - after.files),
        len(before.images - after.images),
        len(before.blobs - after.blobs),
    )
    assert deleted == (removal.lines, removal.files, removal.images, removal.blobs)


def rm(h: Harness, *texts: str, channel_mode: bool = False) -> Removal:
    removal = plan(h, *texts, channel_mode=channel_mode)
    apply(h, removal, *texts)
    swept(h, removal)
    return removal


def stored(h: Harness, team: str, channel: str) -> set[str]:
    return set(h.archive.stored_messages(team, channel))


def item(kind: str, channel: str = CHANNEL, ts: str | None = None) -> tuple[str, str, str, str]:
    return (TEAM, kind, channel, ts or "")


def keys(items: list[TrackedItem]) -> list[tuple[str, str, str, str]]:
    return [item.key for item in items]


def file_ids(h: Harness, team: str = TEAM) -> set[str]:
    return set(h.archive.directory_state(team, "file"))


def image_ids(h: Harness, team: str = TEAM) -> set[str]:
    return set(h.archive.directory_state(team, "image"))


def avatar(user_id: str) -> str:
    [user] = [u for u in spike("users.list")["members"] if u["id"] == user_id]
    return user["profile"]["image_512"]


def test_rm_conversation_deletes_its_lines_and_unreferenced_files(h: Harness):
    pull_and_download(h, BASE, SECOND_LINK)
    assert file_ids(h) == {*SPIKE_FILES, TOMBSTONE_FILE}
    assert GIPHY_URL in image_ids(h)

    removal = rm(h, BASE)

    assert keys(removal.items) == [item("conversation")]
    assert (removal.teams, removal.wiped_teams) == ([TEAM], [])
    assert (removal.lines, removal.files) == (CHANNEL_MESSAGES, len(SPIKE_FILES) + 1)
    assert removal.kept_messages == 0
    assert stored(h, TEAM, CHANNEL) == set()
    assert stored(h, TEAM, SECOND_CHANNEL) == {ts_on(18, 9)}
    assert file_ids(h) == set()
    assert GIPHY_URL not in image_ids(h)
    assert avatar(ME) in image_ids(h)
    assert removal.blobs > 0
    run = h.last_run()
    assert (run.command, run.teams) == ("rm", [TEAM])
    assert [(i.key, i.status) for i in run.tracked] == [(item("conversation"), "removed")]
    removed = {d.target for d in run.downloads if d.status == "removed"}
    assert {*SPIKE_FILES, TOMBSTONE_FILE} <= removed
    assert GIPHY_URL in removed
    assert avatar(ME) not in removed
    assert run.counts == {
        "lines_deleted": removal.lines,
        "files_deleted": removal.files,
        "images_deleted": removal.images,
        "blobs_deleted": removal.blobs,
    }


def test_rm_thread_inside_tracked_conversation_keeps_its_messages(h: Harness):
    pull_and_download(h, BASE, THREAD_LINK)
    before = inventory(h)

    removal = rm(h, THREAD_LINK)

    assert keys(removal.items) == [item("thread", ts=PARENT_TS)]
    assert (removal.lines, removal.files, removal.images, removal.blobs) == (0, 0, 0, 0)
    assert removal.kept_messages == len(THREAD_TS)
    assert removal.wiped_teams == []
    assert inventory(h) == before
    assert keys(h.tracked()) == [item("conversation")]


def test_rm_conversation_keeps_its_tracked_threads(h: Harness):
    pull_and_download(h, BASE, THREAD_LINK)

    removal = rm(h, BASE)

    assert removal.lines == CHANNEL_MESSAGES - len(THREAD_TS)
    assert removal.kept_messages == len(THREAD_TS)
    assert stored(h, TEAM, CHANNEL) == set(THREAD_TS)
    assert file_ids(h) == {THREAD_FILE}
    assert GIPHY_URL in image_ids(h)
    assert keys(h.tracked()) == [item("thread", ts=PARENT_TS)]


def test_rm_workspace_wipes_its_directory(h: Harness):
    pull_and_download(h, BASE, SECOND_LINK, OTHER_LINK)
    other_lines = [env for env in h.archive.lines if env.meta.team == OTHER_TEAM]

    removal = rm(h, "naleidworkspace")

    assert set(keys(removal.items)) == {item("conversation"), item("conversation", SECOND_CHANNEL)}
    assert (removal.teams, removal.wiped_teams) == ([TEAM], [TEAM])
    assert removal.lines == CHANNEL_MESSAGES + 1
    assert [env for env in h.archive.lines if env.meta.team == TEAM] == []
    assert [env for env in h.archive.lines if env.meta.team == OTHER_TEAM] == other_lines
    assert [i.team_id for i in h.tracked()] == [OTHER_TEAM]


def test_rm_last_item_in_workspace_wipes_its_directory(h: Harness):
    pull_and_download(h, BASE)

    removal = rm(h, "#all-naleid")

    assert keys(removal.items) == [item("conversation")]
    assert removal.wiped_teams == [TEAM]
    assert removal.lines == CHANNEL_MESSAGES
    assert [env for env in h.archive.lines if env.meta.kind != "run"] == []
    assert h.blobs.blobs == {}
    assert h.tracked() == []


def test_rm_keeps_blob_another_workspace_references(h: Harness):
    shared = message(ts_on(18, 9), attachments=[{"image_url": GIPHY_URL}])
    h.connector.gateways[OTHER].conversations[OTHER_CHANNEL] = [shared]
    pull_and_download(h, BASE, OTHER_LINK)
    [giphy_blob] = {
        env.meta.blob for env in h.archive.lines if env.meta.id == GIPHY_URL and env.meta.blob
    }

    removal = rm(h, "naleidworkspace")

    assert removal.wiped_teams == [TEAM]
    assert giphy_blob in h.blobs.blobs
    assert image_ids(h, OTHER_TEAM) >= {GIPHY_URL}


def test_pull_after_rm_does_not_requeue_removed_downloads(h: Harness):
    h.pull(BASE, SECOND_LINK)
    queued = {job.target for job in h.downloader.jobs}
    assert GIPHY_URL in queued and THREAD_FILE in queued

    rm(h, BASE)
    h.downloader.jobs.clear()
    h.pull()

    requeued = {job.target for job in h.downloader.jobs}
    assert GIPHY_URL not in requeued
    assert not requeued & set(SPIKE_FILES)
    assert avatar(ME) in requeued


def test_rm_rerun_after_interrupted_sweep_finishes_it(h: Harness):
    pull_and_download(h, BASE, SECOND_LINK)
    first = plan(h, BASE)
    apply(h, first, BASE)
    assert len(stored(h, TEAM, CHANNEL)) == CHANNEL_MESSAGES

    again = plan(h, BASE)

    assert again.items == []
    assert keys(again.already_removed) == [item("conversation")]
    assert not again.nothing_to_do
    assert (again.lines, again.files, again.images, again.blobs) == (
        first.lines,
        first.files,
        first.images,
        first.blobs,
    )
    apply(h, again, BASE)
    assert h.last_run().downloads == []
    swept(h, again)
    assert stored(h, TEAM, CHANNEL) == set()
    assert plan(h, BASE).nothing_to_do


def test_rm_of_untracked_selector_fails(h: Harness):
    with pytest.raises(SlarcError, match=r"^Nothing tracked matches #all-naleid\.$"):
        plan(h, "#all-naleid")
    h.pull(BASE)

    with pytest.raises(SlarcError, match=r"^Nothing tracked matches #nowhere\.$"):
        plan(h, "#nowhere")
    with pytest.raises(SlarcError) as raised:
        plan(h, SNIPPET_LINK)
    assert str(raised.value) == f"Nothing tracked matches {SNIPPET_LINK}."
    with pytest.raises(SlarcError, match=r"^Nothing tracked matches other\.$"):
        plan(h, "other")


def test_rm_reply_link_removes_its_thread_item(h: Harness):
    h.pull(BASE, THREAD_LINK)

    removal = plan(h, REPLY_LINK)

    assert keys(removal.items) == [item("thread", ts=PARENT_TS)]


def test_rm_message_link_to_a_reply_names_its_thread(h: Harness):
    h.pull(BASE, THREAD_LINK)

    removal = plan(h, REPLY_MESSAGE_LINK)

    assert keys(removal.items) == [item("thread", ts=PARENT_TS)]


def test_rm_keeps_replies_stored_under_a_tracked_message_item(h: Harness):
    parent, reply_ts = ts_on(18, 9), ts_on(18, 10)
    h.pull(BASE, f"{SECOND_LINK}/p{parent.replace('.', '')}")
    assert item("message", SECOND_CHANNEL, parent) in keys(h.tracked())
    thread_file = spike(f"files.info.{THREAD_FILE}")["file"]
    reply = message(reply_ts, thread_ts=parent, files=[thread_file])
    h.gateway.conversations[SECOND_CHANNEL].append(reply)
    pull_and_download(h)
    assert reply_ts in stored(h, TEAM, SECOND_CHANNEL)

    rm(h, BASE)

    assert stored(h, TEAM, SECOND_CHANNEL) == {parent, reply_ts}
    assert file_ids(h) == {THREAD_FILE}


def test_link_less_selector_names_the_only_tracked_workspace(h: Harness):
    h.pull(BASE, OTHER_LINK)
    rm(h, "other")

    removal = plan(h, "#all-naleid")

    assert keys(removal.items) == [item("conversation")]


def test_rm_message_link_names_its_thread_and_message_items(h: Harness):
    h.pull(BASE)
    message_item = TrackedItem(team_id=TEAM, kind="message", channel=CHANNEL, ts=PARENT_TS)
    thread_item = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT_TS)
    later = "2026-10-20T00:00:00Z"
    run = RunRecord(run="r_20261020T000000Z", command="pull", started=later, finished=later)
    run.tracked = [message_item, thread_item]
    h.archive.append_run(run, __version__)

    removal = plan(h, THREAD_LINK)

    assert set(keys(removal.items)) == {message_item.key, thread_item.key}


def test_rm_channel_mode_widens_a_message_link(h: Harness):
    h.pull(BASE)

    removal = plan(h, SNIPPET_LINK, channel_mode=True)

    assert keys(removal.items) == [item("conversation")]


def test_retracked_item_starts_a_fresh_backfill(h: Harness):
    h.pull(BASE)
    rm(h, BASE)
    assert h.tracked() == []
    h.gateway.calls.clear()

    h.pull(BASE)

    histories = [call[1:] for call in h.gateway.calls if call[0] == "conversations.history"]
    assert histories and set(histories) == {(CHANNEL, None, None)}
    assert len(stored(h, TEAM, CHANNEL)) == CHANNEL_MESSAGES
    [retracked] = h.tracked()
    assert (retracked.key, retracked.status) == (item("conversation"), "ok")


def test_rm_never_calls_slack(h: Harness):
    pull_and_download(h, BASE, THREAD_LINK)
    h.gateway.calls.clear()
    h.connector.connects.clear()

    rm(h, BASE)
    rm(h, THREAD_LINK)

    assert h.gateway.calls == []
    assert h.connector.connects == []
