# ABOUTME: Tests for importing a JSONL archive into a lake and exporting a lake back to JSONL.
# ABOUTME: Folds over the same JSONL files, through archive_state, are the oracle for the lake.
import hashlib
from pathlib import Path

import msgspec
import pytest

from slarc import archive_state
from slarc.adapters import jsonl_import
from slarc.adapters.jsonl_import import (
    DIRECTORY_FILES,
    IMPORTED_REVISION,
    RUNS_FILE,
    ImportReport,
    export_jsonl,
    has_jsonl,
    import_jsonl,
    read_envelopes,
)
from slarc.adapters.lake import CATALOG, DATA_DIR, connect_lake
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.envelope import Envelope, Kind, encode_line, month_of
from slarc.domain.tracking import run_envelope
from slarc.ports import ArchiveStore, Commit, SlarcError
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


def imported(items: dict) -> dict:
    return {
        key: msgspec.structs.replace(item, revision=IMPORTED_REVISION)
        for key, item in items.items()
    }


class JsonlFolds:
    """The state of a JSONL archive read through archive_state's folds, without any store.

    Its tracked and removed items carry the revision the import gives them.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        runs = archive_state.latest_runs(read_envelopes([root / RUNS_FILE]))
        self._runs = runs

    def tracked_items(self) -> dict:
        return imported(self._runs.tracked_items())

    def removed_items(self) -> dict:
        return imported(self._runs.removed_items())

    def channels(self, team: str) -> list[str]:
        raw = self._root / "raw" / team
        return sorted(d.name for d in raw.glob("*") if any(d.glob("*.jsonl")))

    def message_lines(self, team: str, channel: str) -> list[Envelope]:
        months = sorted((self._root / "raw" / team / channel).glob("*.jsonl"))
        return [env for env in read_envelopes(months) if env.meta.kind in ("message", "deleted")]

    def stored_messages(self, team: str, channel: str) -> dict:
        latest = archive_state.LatestMessages()
        for env in self.message_lines(team, channel):
            latest.add(env)
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict:
        return archive_state.directory_state(self._directory_lines(team, kind))

    def directory_objects(self, team: str, kind: Kind) -> dict:
        return archive_state.directory_objects(self._directory_lines(team, kind))

    def outstanding_downloads(self) -> dict:
        """The download states that are not removed and whose target has no blob."""
        outstanding = {}
        for key, state in self._runs.downloads().items():
            held = self.directory_state(state.team_id, state.target_kind).get(state.target)
            if state.status != "removed" and (held is None or held.blob is None):
                outstanding[key] = state
        return outstanding

    def _directory_lines(self, team: str, kind: Kind) -> list[Envelope]:
        path = self._root / "directory" / team / DIRECTORY_FILES[kind]
        return list(read_envelopes([path]))


def assert_same_reads(
    lake: ArchiveStore, expected: ArchiveStore | JsonlFolds, teams: list[str]
) -> None:
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


def jsonl_hashes(root: Path) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.jsonl"))
    }


def jsonl_path(root: Path, env: Envelope) -> Path:
    meta = env.meta
    if meta.kind == "run":
        return root / RUNS_FILE
    if meta.kind in ("message", "deleted"):
        return (
            root / "raw" / str(meta.team) / str(meta.channel) / f"{month_of(meta.ts or '0')}.jsonl"
        )
    return root / "directory" / str(meta.team) / DIRECTORY_FILES[meta.kind]


def write_jsonl(root: Path, *lines: Envelope, runs=()) -> None:
    for env in [*lines, *(run_envelope(run, VERSION) for run in runs)]:
        path = jsonl_path(root, env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as out:
            out.write(encode_line(env))


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

    expected = JsonlFolds(root)
    lake = DuckLakeArchiveStore(root, ext_dir)
    assert_same_reads(lake, expected, teams_of(root))
    assert lake.downloads() == expected.outstanding_downloads()
    assert report == ImportReport(
        messages=44,
        directory=65,
        runs=1,
        tracked=5,
        removed=0,
        downloads=len(lake.downloads()),
        duplicates=0,
    )
    lake.close()


def test_import_report_counts_removed_items_apart(tmp_path: Path, ext_dir: Path) -> None:
    removed = msgspec.structs.replace(tracked("200.000000", "C0OTHER0001"), status="removed")
    write_jsonl(tmp_path, runs=[run_record(R1, T1, tracked=[tracked("100.000000"), removed])])

    report = import_jsonl(tmp_path, ext_dir)

    assert (report.tracked, report.removed) == (1, 1)


def test_import_leaves_out_download_states_whose_target_has_a_blob(
    tmp_path: Path, ext_dir: Path
) -> None:
    write_jsonl(
        tmp_path,
        file_line("F1", T1, R1, None),
        file_line("F1", T2, R2, "ab" * 32),
        file_line("F2", T1, R1, None),
        runs=[
            run_record(
                R1, T1, downloads=[download("F1", "pending", 0), download("F2", "pending", 0)]
            )
        ],
    )

    report = import_jsonl(tmp_path, ext_dir)

    lake = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert list(lake.downloads()) == [(TEAM, "F2")]
    assert lake.downloads() == JsonlFolds(tmp_path).outstanding_downloads()
    assert report.downloads == 1
    lake.close()


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
    lake = DuckLakeArchiveStore(root, ext_dir)
    assert_same_reads(lake, JsonlFolds(root), teams_of(root))
    lake.close()


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
    lake.commit(Commit(items=[later], downloads=downloads))
    lake.append_run(run_record(R3, T3), VERSION)
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
