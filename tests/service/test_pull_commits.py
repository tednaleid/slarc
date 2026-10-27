# ABOUTME: Tests for where pull commits: before an item is read, per directory refresh, per page
# ABOUTME: and per item, and how a commit rolls back when another command changed what it read.
from collections.abc import Iterator

import msgspec
import pytest

from slarc.domain.tracking import ItemKey, TrackedItem
from slarc.ports import AuthFailed, Commit, DownloadJob, DownloadResult
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    NALEID,
    OTHER,
    OTHER_CHANNEL,
    OTHER_LINK,
    OTHER_TEAM,
    PARENT_TS,
    TEAM,
    Harness,
    message,
    other_gateway,
    ts_on,
)

SECOND_CHANNEL = "C0SECOND001"
SECOND_LINK = f"https://naleidworkspace.slack.com/archives/{SECOND_CHANNEL}"
THREAD_LINK = f"{BASE}/p1792410693663869"
THREAD = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT_TS)
FILE_ID = "F0FILE00001"
FILE_URL = f"https://files.slack.com/files-pri/{TEAM}-{FILE_ID}/notes.txt"
FILE = {
    "id": FILE_ID,
    "name": "notes.txt",
    "mode": "hosted",
    "size": 5,
    "url_private_download": FILE_URL,
}
CONFLICT = "Another slarc command changed the archive at the same time."


@pytest.fixture
def h() -> Harness:
    return Harness()


def conversation(channel: str, team: str = TEAM) -> ItemKey:
    return (team, "conversation", channel, "")


def unfinished(channel: str, error: str = CONFLICT) -> str:
    return f"Could not finish pulling conversation {channel}; the next pull resumes it: {error}"


def stored(h: Harness, channel: str) -> set[str]:
    return set(h.archive.stored_messages(TEAM, channel))


def history_calls(h: Harness) -> list[tuple[str | None, ...]]:
    return [call[2:] for call in h.gateway.calls if call[0] == "conversations.history"]


def interrupt() -> None:
    raise KeyboardInterrupt


def remove_conversation(h: Harness, channel: str, files: set[str] | None = None) -> None:
    """Commits, as rm would from another command, the conversation's removal: its messages, the
    given files' directory rows and download states, and the item set to removed."""
    with h.archive.elsewhere() as archive:
        item = archive.tracked_items()[conversation(channel)]
        archive.drop_messages(TEAM, channel, set(archive.stored_messages(TEAM, channel)))
        archive.drop_directory_lines(TEAM, "file", files or set())
        archive.drop_downloads(TEAM, files or set())
        archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))


def remove_workspace(h: Harness, team: str) -> None:
    """Commits, as rm would from another command, the removal of everything in the workspace."""
    with h.archive.elsewhere() as archive:
        items = [item for item in archive.tracked_items().values() if item.team_id == team]
        archive.drop_workspace(team)
        archive.commit(
            Commit(items=[msgspec.structs.replace(item, status="removed") for item in items])
        )


def test_interrupted_backfill_keeps_committed_pages_and_resumes(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(7)]
    h.serve(stamps, page_size=2)
    h.gateway.before_history_page(3, interrupt)

    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)

    assert stored(h, CHANNEL) == set(stamps[3:])
    [partial] = h.tracked()
    assert (partial.newest_ts, partial.oldest_ts) == (stamps[6], stamps[3])
    assert partial.backfill_complete is False

    h.gateway.calls.clear()
    h.pull()

    assert stored(h, CHANNEL) == set(stamps)
    assert history_calls(h) == [(None, stamps[3]), (None, stamps[3]), (stamps[6], None)]
    [item] = h.tracked()
    assert (item.newest_ts, item.oldest_ts, item.backfill_complete) == (stamps[6], stamps[0], True)


def test_pull_commit_after_rm_of_its_item_rolls_back(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(5)]
    h.serve(stamps, page_size=2)
    h.change_message(stamps[1], files=[FILE])
    second_ts = ts_on(18, 9)
    h.gateway.conversations[SECOND_CHANNEL] = [message(second_ts)]
    h.gateway.before_history_page(2, lambda: remove_conversation(h, CHANNEL))

    report = h.pull(BASE, SECOND_LINK)

    assert stored(h, CHANNEL) == set()
    assert list(h.archive.removed_items()) == [conversation(CHANNEL)]
    assert FILE_ID not in h.archive.directory_state(TEAM, "file")
    assert (TEAM, FILE_ID) not in h.archive.downloads()
    assert report.skipped == [unfinished(CHANNEL)]
    [second] = h.tracked()
    assert (second.channel, second.backfill_complete) == (SECOND_CHANNEL, True)
    assert stored(h, SECOND_CHANNEL) == {second_ts}


def test_second_pull_of_one_item_rolls_back(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(5)]
    h.serve(stamps, page_size=2)
    other_pull_at = "2026-10-19T13:45:00Z"

    def other_pull_advances_the_item() -> None:
        with h.archive.elsewhere() as archive:
            item = archive.tracked_items()[conversation(CHANNEL)]
            advanced = msgspec.structs.replace(item, last_pull_at=other_pull_at)
            archive.commit(Commit(items=[advanced]))

    h.gateway.before_history_page(2, other_pull_advances_the_item)

    report = h.pull(BASE)

    assert stored(h, CHANNEL) == set(stamps[3:])
    [item] = h.tracked()
    assert (item.last_pull_at, item.oldest_ts, item.revision) == (other_pull_at, stamps[3], 3)
    assert report.skipped == [unfinished(CHANNEL)]


def test_missing_required_file_rolls_back_and_next_pull_restores_it(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.gateway.conversations[SECOND_CHANNEL] = [message(ts_on(18, 9), files=[FILE])]
    h.pull(SECOND_LINK)
    assert FILE_ID in h.archive.directory_state(TEAM, "file")

    parent = ts_on(18, 2)
    stamps = [ts_on(18, 1), parent, ts_on(18, 3)]
    h.gateway.conversations[CHANNEL] = [
        message(stamps[0], files=[FILE]),
        message(parent, thread_ts=parent, reply_count=1),
        message(stamps[2], thread_ts=parent),
    ]
    served = h.gateway.replies

    def rm_of_second_channel_commits_first(channel: str, ts: str) -> list[dict]:
        monkeypatch.setattr(h.gateway, "replies", served)
        remove_conversation(h, SECOND_CHANNEL, files={FILE_ID})
        return served(channel, ts)

    monkeypatch.setattr(h.gateway, "replies", rm_of_second_channel_commits_first)
    report = h.pull(BASE)

    assert stored(h, CHANNEL) == set()
    assert FILE_ID not in h.archive.directory_state(TEAM, "file")
    assert (TEAM, FILE_ID) not in h.archive.downloads()
    assert report.skipped == [unfinished(CHANNEL)]

    h.pull()

    assert stored(h, CHANNEL) == set(stamps)
    assert FILE_ID in h.archive.directory_state(TEAM, "file")
    assert h.archive.downloads()[(TEAM, FILE_ID)].status == "pending"


def test_directory_refresh_after_workspace_removal_skips_the_workspace(h: Harness):
    other = other_gateway()
    h.connector.gateways = {NALEID: h.gateway, OTHER: other}
    h.serve([ts_on(18, 9)])
    h.pull(BASE, OTHER_LINK)
    other.conversations[OTHER_CHANNEL].append(message(ts_on(19, 9)))
    h.gateway.before_history_page(1, lambda: remove_workspace(h, OTHER_TEAM))

    report = h.pull()

    assert report.skipped == [
        f"Workspace {OTHER_TEAM} was removed while this pull was running, so its tracked items "
        "were not pulled."
    ]
    assert list(h.archive.removed_items()) == [conversation(OTHER_CHANNEL, OTHER_TEAM)]
    assert [env for env in h.archive.lines if env.meta.team == OTHER_TEAM] == []


def test_workspace_skipped_mid_pull_keeps_none_of_its_open_batch(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    def rejected(channel: str, ts: str) -> list[dict]:
        raise AuthFailed("desktop", "naleidworkspace")

    monkeypatch.setattr(h.gateway, "replies", rejected)
    h.pull(BASE)

    assert stored(h, CHANNEL) == set()
    [item] = h.tracked()
    assert (item.channel, item.backfill_complete) == (CHANNEL, False)


def test_item_tracked_elsewhere_first_is_left_out_with_a_warning(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    served = h.gateway.replies

    def another_pull_tracks_it_first(channel: str, ts: str) -> list[dict]:
        monkeypatch.setattr(h.gateway, "replies", served)
        with h.archive.elsewhere() as archive:
            archive.commit(Commit(items=[THREAD]))
        return served(channel, ts)

    monkeypatch.setattr(h.gateway, "replies", another_pull_tracks_it_first)
    report = h.pull(THREAD_LINK)

    assert report.items_selected == 0
    assert report.skipped == [f"Could not track {THREAD_LINK}, so it was not pulled: {CONFLICT}"]
    assert h.tracked() == [msgspec.structs.replace(THREAD, revision=1)]


def test_thread_item_removed_during_the_pull_is_skipped(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.pull(THREAD_LINK, BASE)
    listed = h.gateway.users

    def rm_of_the_thread_commits_first() -> Iterator[dict]:
        monkeypatch.setattr(h.gateway, "users", listed)
        with h.archive.elsewhere() as archive:
            item = archive.tracked_items()[THREAD.key]
            archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))
        return listed()

    monkeypatch.setattr(h.gateway, "users", rm_of_the_thread_commits_first)
    h.clock.advance(86400)
    report = h.pull()

    assert report.skipped == [
        f"Could not finish pulling #all-naleid thread {PARENT_TS}; the next pull resumes it: "
        f"{CONFLICT}"
    ]
    assert list(h.archive.removed_items()) == [THREAD.key]
    assert report.items_pulled == 1


def test_drain_after_rm_of_the_item_drops_its_file_result(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.gateway.conversations[CHANNEL] = [message(ts_on(18, 9), files=[FILE])]
    h.gateway.conversations[SECOND_CHANNEL] = [message(ts_on(18, 10))]
    h.gateway.files[FILE_ID] = FILE
    h.downloader.serve(FILE_URL, b"notes")
    fetch = h.downloader.fetch

    def rm_commits_before_the_drain(
        jobs: list[DownloadJob], parallelism: int
    ) -> Iterator[DownloadResult]:
        remove_conversation(h, CHANNEL, files={FILE_ID})
        return fetch(jobs, parallelism)

    monkeypatch.setattr(h.downloader, "fetch", rm_commits_before_the_drain)

    report = h.pull(BASE, SECOND_LINK)

    assert FILE_ID in {job.target for job in h.downloader.jobs}
    assert list(h.archive.removed_items()) == [conversation(CHANNEL)]
    assert FILE_ID not in h.archive.directory_state(TEAM, "file")
    assert (TEAM, FILE_ID) not in h.archive.downloads()
    assert report.files_saved == 0
