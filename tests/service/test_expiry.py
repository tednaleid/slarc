# ABOUTME: Tests for expiry: pull fixtures into an in-memory archive, set a retention, move the
# ABOUTME: clock on, then check what expires: whole threads, then files and images only they used.
import msgspec
import pytest

from slarc import __version__
from slarc.domain.envelope import Envelope, Meta, fetched_at
from slarc.domain.tracking import DownloadState, WorkspaceSetting
from slarc.ports import Commit
from slarc.service import expiry
from slarc.service.expiry import Expiry, expire
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    SPIKE_FILES,
    TEAM,
    Harness,
    message,
    ts_on,
)
from tests.service.test_remove import (
    SECOND_CHANNEL,
    SECOND_LINK,
    THREAD_FILE,
    avatar,
    file_ids,
    image_ids,
    pull_and_download,
    stored,
)

PARENT = ts_on(17, 9)
REPLY = ts_on(19, 13)
LONE = ts_on(18, 9)
SECOND_KEY = (TEAM, "conversation", SECOND_CHANNEL, "")
DAY = 86400


@pytest.fixture
def h() -> Harness:
    h = Harness()
    h.gateway.conversations[SECOND_CHANNEL] = [
        message(PARENT, thread_ts=PARENT, reply_count=1),
        message(REPLY, thread_ts=PARENT),
        message(LONE),
    ]
    return h


def run_expiry(h: Harness) -> tuple[Expiry, UnitOfWork]:
    uow = UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__)
    with uow:
        result = expire(uow, [TEAM], h.blobs, h.reporter)
    return result, uow


def set_workspace_retention(h: Harness, days: int) -> None:
    held = h.archive.workspace_settings().get(TEAM, WorkspaceSetting(team_id=TEAM))
    h.archive.commit(Commit(settings=[msgspec.structs.replace(held, retention=days)]))


def set_item_retention(h: Harness, key: tuple[str, str, str, str], retention: object) -> None:
    item = h.archive.tracked_items()[key]
    h.archive.commit(Commit(items=[msgspec.structs.replace(item, retention=retention)]))


def content(h: Harness) -> list[Envelope]:
    return [env for env in h.archive.lines if env.meta.kind != "run"]


def lines_of(h: Harness, ts: str) -> list[Envelope]:
    return [env for env in h.archive.lines if env.meta.ts == ts]


def test_a_thread_expires_whole_once_its_newest_reply_passes_the_cutoff(h: Harness):
    h.pull(SECOND_LINK)
    deleted = Meta(
        kind="deleted",
        team=TEAM,
        channel=SECOND_CHANNEL,
        ts=LONE,
        fetched_at=fetched_at(h.clock.now()),
        run="r_marker",
    )
    h.archive.append(Envelope(meta=deleted))
    h.archive.commit(Commit())
    assert len(lines_of(h, LONE)) == 2
    set_workspace_retention(h, 1)
    h.clock.current = h.clock.current.replace(day=20, hour=12)

    first, uow = run_expiry(h)

    assert stored(h, TEAM, SECOND_CHANNEL) == {PARENT, REPLY}
    assert lines_of(h, LONE) == []
    assert (first.messages, first.files, first.images) == (1, 0, 0)
    assert uow.run.counts["messages_expired"] == 1

    h.clock.advance(DAY)
    second, _ = run_expiry(h)

    assert stored(h, TEAM, SECOND_CHANNEL) == set()
    assert second.messages == 2
    assert list(h.archive.tracked_items()) == [SECOND_KEY]


def test_without_retention_nothing_expires(h: Harness):
    pull_and_download(h, BASE, SECOND_LINK)
    before = content(h)
    h.clock.advance(365 * DAY)

    result, _ = run_expiry(h)

    assert (result.messages, result.files, result.images) == (0, 0, 0)
    assert content(h) == before


def test_expired_messages_take_the_files_and_images_only_they_used(h: Harness):
    h.gateway.conversations[SECOND_CHANNEL] = [
        message(LONE, files=[{"id": THREAD_FILE, "mode": "hosted"}])
    ]
    pull_and_download(h, BASE, SECOND_LINK)
    files_before = file_ids(h)
    assert files_before >= {*SPIKE_FILES, THREAD_FILE}
    set_workspace_retention(h, 1)
    set_item_retention(h, SECOND_KEY, "forever")
    h.clock.advance(5 * DAY)

    result, _ = run_expiry(h)

    assert stored(h, TEAM, CHANNEL) == set()
    assert stored(h, TEAM, SECOND_CHANNEL) == {LONE}
    assert file_ids(h) == {THREAD_FILE}
    assert avatar(ME) in image_ids(h)
    assert result.files == len(files_before) - 1
    assert result.images > 0
    assert all(state.target == THREAD_FILE for state in h.download_states())


def test_expiry_drops_queued_downloads_of_what_it_expired(h: Harness):
    h.pull(BASE)
    set_workspace_retention(h, 1)
    h.clock.advance(5 * DAY)
    queued = DownloadState(
        team_id=TEAM, target_kind="file", target=SPIKE_FILES[0], status="pending"
    )

    uow = UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__)
    with uow:
        uow.run.downloads.append(queued)
        expire(uow, [TEAM], h.blobs, h.reporter)
        assert queued not in uow.run.downloads


def test_a_slack_archived_conversation_still_expires(h: Harness):
    h.gateway.infos[SECOND_CHANNEL] = {"id": SECOND_CHANNEL, "is_archived": True}
    h.pull(SECOND_LINK)
    h.pull()
    assert h.archive.tracked_items()[SECOND_KEY].archived
    set_workspace_retention(h, 1)
    h.clock.advance(5 * DAY)

    run_expiry(h)

    assert stored(h, TEAM, SECOND_CHANNEL) == set()


def test_expiry_retries_with_fresh_reads_when_a_retention_changes_meanwhile(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.pull(SECOND_LINK)
    set_workspace_retention(h, 1)
    h.clock.advance(5 * DAY)
    plan_deletion = expiry.plan_deletion
    calls = []

    def lengthen_first(*args, **kwargs):
        calls.append(1)
        planned = plan_deletion(*args, **kwargs)
        if len(calls) == 1:
            with h.archive.elsewhere():
                set_workspace_retention(h, 30)
        return planned

    monkeypatch.setattr(expiry, "plan_deletion", lengthen_first)

    result, _ = run_expiry(h)

    assert len(calls) == 2
    assert result.messages == 0
    assert stored(h, TEAM, SECOND_CHANNEL) == {PARENT, REPLY, LONE}


def test_expiry_retries_when_an_item_changes_meanwhile(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.pull(SECOND_LINK)
    set_workspace_retention(h, 1)
    h.clock.advance(5 * DAY)
    plan_deletion = expiry.plan_deletion
    calls = []

    def keep_forever_first(*args, **kwargs):
        calls.append(1)
        planned = plan_deletion(*args, **kwargs)
        if len(calls) == 1:
            with h.archive.elsewhere():
                set_item_retention(h, SECOND_KEY, "forever")
        return planned

    monkeypatch.setattr(expiry, "plan_deletion", keep_forever_first)

    run_expiry(h)

    assert len(calls) == 2
    assert stored(h, TEAM, SECOND_CHANNEL) == {PARENT, REPLY, LONE}


def test_expiry_gives_up_with_a_warning_when_other_commands_keep_changing_the_archive(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    h.pull(SECOND_LINK)
    set_workspace_retention(h, 1)
    h.clock.advance(5 * DAY)
    plan_deletion = expiry.plan_deletion

    def always_change(*args, **kwargs):
        planned = plan_deletion(*args, **kwargs)
        with h.archive.elsewhere():
            item = h.archive.tracked_items()[SECOND_KEY]
            h.archive.commit(Commit(items=[item]))
        return planned

    monkeypatch.setattr(expiry, "plan_deletion", always_change)

    result, _ = run_expiry(h)

    assert result.messages == 0
    assert stored(h, TEAM, SECOND_CHANNEL) == {PARENT, REPLY, LONE}
    assert any("expire" in warning for warning in h.reporter.warnings)


def test_expiry_never_wipes_a_workspace_with_nothing_tracked(h: Harness):
    h.pull(SECOND_LINK)
    item = h.archive.tracked_items()[SECOND_KEY]
    h.archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))
    before = content(h)

    run_expiry(h)

    assert content(h) == before
