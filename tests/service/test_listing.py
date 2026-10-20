# ABOUTME: Tests for the list use cases: tracked items in a lake of the recorded archive, and the
# ABOUTME: conversations a fake Slack session can see, marked tracked or not.
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.config import load_settings, read_global_config
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem
from slarc.service.listing import ArchiveListing, ListRow, list_archive, list_remote
from tests.fakes import FakeConnector, FakeSlackGateway
from tests.recorded_fixtures import RECORDED_TEAM, recorded_lake
from tests.service.pull_harness import NALEID, spike

WAVE_PARENT = "1792410693.663869"
LONE_TS = "1792422552.941749"
GIPHY = (
    "https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif?cid=6104955ee4adf136dcbbrjmn0qdsjgxzsg"
    "a2bbxafvz2c6fe&ep=v1_gifs_translate&rid=giphy.gif&ct=g"
)
AVATAR = (
    "https://secure.gravatar.com/avatar/35c81367929ede827ac3ac736b9d4a0e.jpg?s=512&d=https%3A%2F"
    "%2Fa.slack-edge.com%2Fdf10d%2Fimg%2Favatars%2Fava_0026-512.png"
)
VIDEO = "F0CAF9Y9B18"
PDF = "F0CAK6DP8SY"
LATER = "2026-10-20T00:00:00Z"


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return recorded_lake(tmp_path / "archive")


@pytest.fixture
def store(archive: Path, ext_dir: Path) -> Iterator[DuckLakeArchiveStore]:
    store = DuckLakeArchiveStore(archive, ext_dir)
    yield store
    store.close()


def listing_of(archive: Path, ext_dir: Path, store: DuckLakeArchiveStore) -> ArchiveListing:
    settings = load_settings(archive, {}, {}, {})
    with DuckDbQueryIndex(archive, ext_dir) as index:
        return list_archive(settings, store, index)


def append_run(
    store: DuckLakeArchiveStore,
    tracked: list[TrackedItem],
    downloads: list[DownloadState] | None = None,
) -> None:
    run = RunRecord(
        run="r_20261020T000000Z",
        command="pull",
        started=LATER,
        finished=LATER,
        tracked=tracked,
        downloads=downloads or [],
    )
    store.append_run(run, "0.1.0")


def rows_by_item(listing: ArchiveListing) -> dict[tuple[str, str], ListRow]:
    return {(row.conversation, row.kind): row for row in listing.rows}


def download(
    target_kind: Literal["file", "image"],
    target: str,
    status: Literal["pending", "failed", "forbidden", "too_large"],
) -> DownloadState:
    url = f"https://files.slack.com/{target}" if target_kind == "file" else target
    return DownloadState(
        team_id=RECORDED_TEAM, target_kind=target_kind, target=target, url=url, status=status
    )


def test_list_shows_archive_path_and_config_sources(
    archive: Path, ext_dir: Path, store: DuckLakeArchiveStore, tmp_path: Path
) -> None:
    config = tmp_path / "home" / ".config" / "slarc" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[pull]\ndownloads = 2\n", encoding="utf-8")
    settings = load_settings(archive, read_global_config({}, tmp_path / "home"), {}, {})
    with DuckDbQueryIndex(archive, ext_dir) as index:
        listing = list_archive(settings, store, index)
    assert listing.archive == archive
    assert listing.config_files == [archive / "slarc.toml", config]


def test_list_counts_messages_and_pending(
    archive: Path, ext_dir: Path, store: DuckLakeArchiveStore
) -> None:
    store.drop_directory_lines(RECORDED_TEAM, "file", {VIDEO, PDF})
    store.drop_directory_lines(RECORDED_TEAM, "image", {GIPHY, AVATAR})
    thread = TrackedItem(
        team_id=RECORDED_TEAM,
        kind="thread",
        channel="C0CA5396G03",
        ts=WAVE_PARENT,
        last_pull_at=LATER,
    )
    downloads = [
        download("file", VIDEO, "pending"),
        download("file", PDF, "too_large"),
        download("image", GIPHY, "forbidden"),
        download("image", AVATAR, "failed"),
    ]
    append_run(store, tracked=[thread], downloads=downloads)

    listing = listing_of(archive, ext_dir, store)

    rows = rows_by_item(listing)
    assert sorted(rows) == [
        ("#all-naleid", "conversation"),
        (f"#all-naleid thread {WAVE_PARENT}", "thread"),
        ("#slarc-fixtures", "conversation"),
        ("#slarc-fixtures-archived", "conversation"),
        ("@Jim", "conversation"),
        ("mpdm-tim--jim--contact-1", "conversation"),
    ]
    channel = rows[("#all-naleid", "conversation")]
    assert channel == ListRow(
        workspace="naleidworkspace",
        conversation="#all-naleid",
        kind="conversation",
        messages=14,
        last_pull="2026-10-19T19:17:09Z",
        pending=0,
        skipped=1,
        status="ok",
    )
    wave = rows[(f"#all-naleid thread {WAVE_PARENT}", "thread")]
    assert (wave.messages, wave.last_pull, wave.pending, wave.skipped) == (4, LATER, 0, 1)
    fixtures = rows[("#slarc-fixtures", "conversation")]
    assert (fixtures.pending, fixtures.skipped) == (1, 1)
    assert rows[("@Jim", "conversation")].messages == 1
    assert (listing.pending, listing.skipped) == (2, 2)


def test_list_tells_apart_two_threads_in_one_conversation(
    archive: Path, ext_dir: Path, store: DuckLakeArchiveStore
) -> None:
    threads = [
        TrackedItem(team_id=RECORDED_TEAM, kind="thread", channel="C0CA5396G03", ts=ts)
        for ts in (WAVE_PARENT, LONE_TS)
    ]
    append_run(store, tracked=threads)

    rows = listing_of(archive, ext_dir, store).rows
    labels = [row.conversation for row in rows if row.kind == "thread"]

    assert labels == [f"#all-naleid thread {WAVE_PARENT}", f"#all-naleid thread {LONE_TS}"]


def test_list_marks_not_visible(archive: Path, ext_dir: Path, store: DuckLakeArchiveStore) -> None:
    hidden = [
        TrackedItem(team_id=RECORDED_TEAM, kind="conversation", channel=channel, status=status)
        for channel, status in (("C0CA7SNHTSP", "not_visible"), ("C0CAGONE001", "not_visible"))
    ]
    append_run(store, tracked=hidden)

    rows = rows_by_item(listing_of(archive, ext_dir, store))

    assert rows[("#slarc-fixtures", "conversation")].status == "not_visible"
    gone = rows[("C0CAGONE001", "conversation")]
    assert (gone.workspace, gone.messages, gone.status) == ("naleidworkspace", 0, "not_visible")
    assert rows[("#all-naleid", "conversation")].status == "ok"


def test_list_remote_marks_tracked(store: DuckLakeArchiveStore) -> None:
    infos = {c["id"]: c for c in spike("conversations.list")["channels"]}
    visible = ["C0CA5396G03", "C0CA53DLVJP", "D0CAD7JE1QR"]
    gateway = FakeSlackGateway({channel: [] for channel in visible}, infos=infos)

    rows = list_remote(FakeConnector({NALEID: gateway}), "naleidworkspace", store)

    assert [(r.workspace, r.channel, r.conversation, r.kind, r.tracked) for r in rows] == [
        ("naleidworkspace", "C0CA5396G03", "#all-naleid", "channel", True),
        ("naleidworkspace", "C0CA53DLVJP", "#new-channel", "channel", False),
        ("naleidworkspace", "D0CAD7JE1QR", "@Jim", "dm", True),
    ]
    assert {call[0] for call in gateway.calls} == {"auth.test", "conversations.list"}
