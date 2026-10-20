# ABOUTME: Tests for importing a JSONL archive into a lake and exporting a lake back to JSONL.
# ABOUTME: The JSONL store reading the same files is the oracle the imported lake must match.
import hashlib
from pathlib import Path

import pytest

from slarc.adapters import jsonl_import
from slarc.adapters.jsonl_import import (
    DIRECTORY_FILES,
    ImportReport,
    export_jsonl,
    has_jsonl,
    import_jsonl,
)
from slarc.adapters.jsonl_store import JsonlArchiveStore
from slarc.adapters.lake import CATALOG, DATA_DIR, connect_lake
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.envelope import encode_line
from slarc.ports import ArchiveStore, SlarcError
from slarc.service.downloads import outstanding_downloads
from tests.contract.test_archive_store import (
    CHANNEL,
    R1,
    R2,
    R3,
    T1,
    T2,
    T3,
    TEAM,
    TS,
    VERSION,
    download,
    file_line,
    message,
    run_record,
    tracked,
)
from tests.recorded_fixtures import copy_recorded

IMPORTING = ("catalog.sqlite.importing", "data.importing")


def teams_of(root: Path) -> list[str]:
    return sorted({p.name for top in ("raw", "directory") for p in (root / top).glob("*")})


def assert_same_reads(lake: ArchiveStore, expected: ArchiveStore, teams: list[str]) -> None:
    assert lake.tracked_items() == expected.tracked_items()
    assert lake.removed_items() == expected.removed_items()
    for team in teams:
        assert lake.channels(team) == expected.channels(team)
        for channel in expected.channels(team):
            assert lake.stored_messages(team, channel) == expected.stored_messages(team, channel)
            assert sorted(map(encode_line, lake.message_lines(team, channel))) == sorted(
                map(encode_line, expected.message_lines(team, channel))
            )
        for kind in DIRECTORY_FILES:
            assert lake.directory_objects(team, kind) == expected.directory_objects(team, kind)
            assert lake.directory_state(team, kind) == expected.directory_state(team, kind)


def outstanding_without_removed(store: ArchiveStore) -> dict:
    return {
        key: state
        for key, state in outstanding_downloads(store).items()
        if state.status != "removed"
    }


def jsonl_hashes(root: Path) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.jsonl"))
    }


def write_jsonl(root: Path, *lines, runs=()) -> None:
    store = JsonlArchiveStore(root)
    for line in lines:
        store.append(line)
    for run in runs:
        store.append_run(run, VERSION)
    store.close()


def test_has_jsonl_looks_for_any_jsonl_part(tmp_path: Path) -> None:
    assert not has_jsonl(tmp_path)
    for name in ("raw", "directory"):
        (tmp_path / name).mkdir()
        assert has_jsonl(tmp_path)
        (tmp_path / name).rmdir()
    (tmp_path / "runs.jsonl").write_text("")
    assert has_jsonl(tmp_path)


def test_import_of_recorded_fixture_matches_jsonl_reads(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")

    report = import_jsonl(root, ext_dir)

    expected = JsonlArchiveStore(root)
    lake = DuckLakeArchiveStore(root, ext_dir)
    assert_same_reads(lake, expected, teams_of(root))
    assert lake.downloads() == outstanding_without_removed(expected)
    assert report == ImportReport(
        messages=44, directory=65, runs=1, tracked=5, downloads=len(lake.downloads()), duplicates=0
    )
    lake.close()
    expected.close()


def test_import_drops_exact_duplicates_and_counts_them(tmp_path: Path, ext_dir: Path) -> None:
    first = message(TS, "Hello", T1, R1)
    edited = message(TS, "Hello!", T2, R2)
    write_jsonl(tmp_path, first, first, edited, first, runs=[run_record(R1, T1)])

    report = import_jsonl(tmp_path, ext_dir)

    lake = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert [env.meta.hash for env in lake.message_lines(TEAM, CHANNEL)] == [
        first.meta.hash,
        edited.meta.hash,
    ]
    assert (report.messages, report.duplicates) == (2, 2)
    lake.close()


def test_import_reads_conflicted_month_copies(tmp_path: Path, ext_dir: Path) -> None:
    later_ts = "1792410800.000200"
    write_jsonl(tmp_path, message(TS, "Hello", T1, R1))
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    write_jsonl(tmp_path / "copy", message(later_ts, "Only in the copy", T2, R2))
    conflicted = month.with_name("2026-10 (Ted's conflicted copy 2026-10-19).jsonl")
    (tmp_path / "copy" / "raw" / TEAM / CHANNEL / "2026-10.jsonl").rename(conflicted)

    import_jsonl(tmp_path, ext_dir)

    lake = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert set(lake.stored_messages(TEAM, CHANNEL)) == {TS, later_ts}
    lake.close()


def test_interrupted_import_is_redone(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")
    (root / "catalog.sqlite.importing").write_bytes(b"left by a killed import")
    (root / "catalog.sqlite.importing-wal").write_bytes(b"left by a killed import")
    (root / "data.importing").mkdir()
    (root / "data.importing" / "stale.parquet").write_bytes(b"stale")
    # A kill between the two renames leaves data/ in place without a catalog.
    (root / DATA_DIR).mkdir()
    (root / DATA_DIR / "orphan.parquet").write_bytes(b"orphan")

    import_jsonl(root, ext_dir)

    assert not any((root / name).exists() for name in IMPORTING)
    assert not (root / "catalog.sqlite.importing-wal").exists()
    assert not (root / DATA_DIR / "orphan.parquet").exists()
    expected = JsonlArchiveStore(root)
    lake = DuckLakeArchiveStore(root, ext_dir)
    assert_same_reads(lake, expected, teams_of(root))
    lake.close()
    expected.close()


def test_import_refuses_an_archive_that_has_a_lake(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")
    import_jsonl(root, ext_dir)

    with pytest.raises(SlarcError):
        import_jsonl(root, ext_dir)
    assert any((root / DATA_DIR).iterdir())


def test_a_mismatch_stops_the_import_before_renaming(
    tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_recorded(tmp_path / "archive")
    insert_rows = jsonl_import.insert_rows

    def without_runs(con, table, columns, rows) -> None:
        insert_rows(con, table, columns, [] if table == "run" else rows)

    monkeypatch.setattr(jsonl_import, "insert_rows", without_runs)

    with pytest.raises(SlarcError, match="run"):
        import_jsonl(root, ext_dir)
    assert not (root / CATALOG).exists()
    assert (root / "catalog.sqlite.importing").exists()


def test_jsonl_files_are_untouched(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")
    before = jsonl_hashes(root)

    import_jsonl(root, ext_dir)

    assert before
    assert jsonl_hashes(root) == before


def test_export_then_import_round_trips(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")
    import_jsonl(root, ext_dir)
    lake = DuckLakeArchiveStore(root, ext_dir)
    lake.append(message(TS, "Hello", T3, R3))
    lake.append(file_line("F1", T3, R3, None))
    later = tracked("100.000000", channel="C0LATER0001")
    downloads = [download("F1", "failed", 1)]
    lake.append_run(run_record(R3, T3, tracked=[later], downloads=downloads), VERSION)
    lake.close()

    exported = tmp_path / "exported"
    export_jsonl(root, ext_dir, exported)
    import_jsonl(exported, ext_dir)

    original = DuckLakeArchiveStore(root, ext_dir)
    copy = DuckLakeArchiveStore(exported, ext_dir)
    assert_same_reads(copy, original, teams_of(exported))
    assert copy.downloads() == original.downloads()
    assert later.key in copy.tracked_items()
    original.close()
    copy.close()


def test_export_keeps_settled_download_states_settled(tmp_path: Path, ext_dir: Path) -> None:
    write_jsonl(
        tmp_path / "archive",
        file_line("F1", T1, R1, None),
        runs=[run_record(R1, T1, downloads=[download("F1", "pending", 0)])],
    )
    root = tmp_path / "archive"
    import_jsonl(root, ext_dir)
    con = connect_lake(root, ext_dir)
    con.execute("DELETE FROM download_state").fetchall()
    con.close()
    lake = DuckLakeArchiveStore(root, ext_dir)
    lake.append_run(run_record(R2, T2), VERSION)
    lake.close()

    export_jsonl(root, ext_dir, tmp_path / "exported")
    import_jsonl(tmp_path / "exported", ext_dir)

    copy = DuckLakeArchiveStore(tmp_path / "exported", ext_dir)
    assert copy.downloads() == {}
    copy.close()
