# ABOUTME: Tests for the archive's owner record: the first open records the host, another host is
# ABOUTME: refused, and a takeover checks the synced files and runs a cautious maintenance.
import shutil
import sqlite3
from pathlib import Path

import duckdb
import pytest

from slarc import bootstrap
from slarc.adapters import duckdb_index, lake_store
from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.lake import (
    CATALOG,
    DATA_DIR,
    OwnedElsewhere,
    SyncNotFinished,
    check_owner,
    connect_lake,
)
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.ports import Commit
from tests.adapters.test_lake_maintenance import data_files, messages
from tests.contract.test_archive_store import CHANNEL, TEAM, TS, tracked
from tests.fakes import FakeArchiveLock

ALPHA = ("machine-alpha", "alpha")
BETA = ("machine-beta", "beta")


def owner_rows(root: Path) -> list[tuple[str, str]]:
    """The owner record, read straight from the catalog file."""
    con = sqlite3.connect(root / CATALOG)
    try:
        rows = con.execute("SELECT machine_id, host_name, since FROM archive_owner").fetchall()
    finally:
        con.close()
    assert all(since for _, _, since in rows)
    return [(machine, host) for machine, host, _ in rows]


def delete_a_row(con: duckdb.DuckDBPyConnection) -> None:
    """Deletes a run row and flushes the inlined deletion into a delete file."""
    con.execute("DELETE FROM run WHERE run = 'r1'")
    con.execute("SELECT count(*) FROM ducklake_flush_inlined_data('lake')").fetchall()


@pytest.fixture
def con(tmp_path: Path, ext_dir: Path):
    con = connect_lake(tmp_path, ext_dir)
    yield con
    con.close()


def test_first_open_records_the_host(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    assert owner_rows(tmp_path) == []
    assert check_owner(con, *ALPHA, take_ownership=False) is False
    assert owner_rows(tmp_path) == [ALPHA]
    assert check_owner(con, *ALPHA, take_ownership=False) is False
    assert owner_rows(tmp_path) == [ALPHA]


def test_other_host_is_refused_with_its_name(
    tmp_path: Path, con: duckdb.DuckDBPyConnection
) -> None:
    check_owner(con, *ALPHA, take_ownership=False)
    with pytest.raises(OwnedElsewhere) as refused:
        check_owner(con, *BETA, take_ownership=False)
    assert str(refused.value) == (
        "This archive is owned by alpha. Wait until your synced folder has finished syncing, "
        "then run the command again with --take-ownership."
    )
    assert owner_rows(tmp_path) == [ALPHA]


def test_owner_is_read_through_a_read_only_connection(tmp_path: Path, ext_dir: Path) -> None:
    connect_lake(tmp_path, ext_dir).close()
    writable = connect_lake(tmp_path, ext_dir)
    check_owner(writable, *ALPHA, take_ownership=False)
    writable.close()
    read_only = connect_lake(tmp_path, ext_dir, read_only=True)
    try:
        assert check_owner(read_only, *ALPHA, take_ownership=False) is False
        with pytest.raises(OwnedElsewhere):
            check_owner(read_only, *BETA, take_ownership=False)
    finally:
        read_only.close()


def test_take_ownership_records_this_host_and_is_cautious(
    tmp_path: Path, con: duckdb.DuckDBPyConnection
) -> None:
    con.execute("INSERT INTO run (run) SELECT 'r' || i FROM range(100) t(i)")
    delete_a_row(con)
    check_owner(con, *ALPHA, take_ownership=False)
    assert check_owner(con, *BETA, take_ownership=True) is True
    assert owner_rows(tmp_path) == [BETA]
    assert check_owner(con, *BETA, take_ownership=False) is False


@pytest.mark.parametrize("missing", ["data file", "delete file"])
def test_take_ownership_with_a_missing_data_file_stops(
    missing: str, tmp_path: Path, con: duckdb.DuckDBPyConnection
) -> None:
    con.execute("INSERT INTO run (run) SELECT 'r' || i FROM range(100) t(i)")
    written = data_files(tmp_path)
    delete_a_row(con)
    deletes = data_files(tmp_path) - written
    assert written and deletes
    check_owner(con, *ALPHA, take_ownership=False)
    for path in written if missing == "data file" else deletes:
        path.unlink()
    with pytest.raises(SyncNotFinished) as stopped:
        check_owner(con, *BETA, take_ownership=True)
    assert str(stopped.value) == (
        "Some of this archive's files have not arrived yet; "
        "wait until your synced folder has finished syncing."
    )
    assert owner_rows(tmp_path) == [ALPHA]


def test_takeover_run_skips_orphan_and_blob_deletion(tmp_path: Path, ext_dir: Path) -> None:
    blobs = FsBlobStore(tmp_path)
    connect_lake(tmp_path, ext_dir).close()
    unreferenced, _ = blobs.write([b"synced ahead of the catalog"])
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock(), owner=lambda con: True)
    for env in messages(50):
        store.append(env)
    store.commit(Commit(items=[tracked(TS)]))
    written = data_files(tmp_path)
    assert written
    store.drop_messages(TEAM, CHANNEL, {env.meta.ts for env in messages(50)})
    store.commit(Commit())
    orphan = tmp_path / DATA_DIR / "main" / "message_version" / "ducklake-synced-ahead.parquet"
    shutil.copyfile(next(iter(written)), orphan)

    assert store.maintain(blobs) is True
    assert written.isdisjoint(data_files(tmp_path))
    assert orphan.exists()
    assert list(blobs.ids()) == [unreferenced]
    store.close()

    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    assert store.maintain(blobs) is True
    assert not orphan.exists()
    assert list(blobs.ids()) == []
    store.close()


def test_only_the_run_that_takes_ownership_is_cautious(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    with bootstrap.archive_stores(root):
        pass
    unreferenced, _ = FsBlobStore(root).write([b"synced ahead of the catalog"])
    bootstrap._owner_checked.clear()
    with bootstrap.archive_stores(root, take_ownership=True) as (archive, blobs):
        assert archive.maintain(blobs) is True
        assert list(blobs.ids()) == [unreferenced]
    bootstrap._owner_checked.clear()
    with bootstrap.archive_stores(root) as (archive, blobs):
        assert archive.maintain(blobs) is True
        assert list(blobs.ids()) == []


def test_one_process_checks_the_owner_once_per_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    with bootstrap.archive_stores(root):
        pass
    calls: list[bool] = []
    real = bootstrap.check_owner

    def spy(con: duckdb.DuckDBPyConnection, machine: str, host: str, take_ownership: bool) -> bool:
        calls.append(take_ownership)
        return real(con, machine, host, take_ownership)

    monkeypatch.setattr(bootstrap, "check_owner", spy)
    bootstrap._owner_checked.clear()
    with bootstrap.archive_stores(root, take_ownership=True) as (_archive, _):
        with bootstrap.query_index(root, take_ownership=True):
            pass
        with bootstrap.archive_stores(root, take_ownership=True):
            pass
    assert calls == [True]
    assert owner_rows(root) == [("test-machine", "test-host")]


def spy_connections(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Records whether each lake connection bootstrap, the store or the index opens is
    read-only."""
    opened: list[bool] = []
    for module in (bootstrap, lake_store, duckdb_index):
        real = module.connect_lake

        def spy(*args, _real=real, **kwargs) -> duckdb.DuckDBPyConnection:
            opened.append(kwargs.get("read_only", False))
            return _real(*args, **kwargs)

        monkeypatch.setattr(module, "connect_lake", spy)
    return opened


def test_an_owned_archive_opens_one_connection_per_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    with bootstrap.archive_stores(root):
        pass
    opened = spy_connections(monkeypatch)

    bootstrap._owner_checked.clear()
    with bootstrap.archive_stores(root):
        pass
    assert opened == [False]

    opened.clear()
    bootstrap._owner_checked.clear()
    with bootstrap.query_index(root):
        pass
    assert opened == [True]

    opened.clear()
    bootstrap._owner_checked.clear()
    with (
        bootstrap.archive_stores(root) as (archive, _),
        bootstrap.shared_index(root, archive) as index,
    ):
        assert archive.tracked_items() == {}
        assert index.conversation_counts() == {}
    assert opened == [False]


def test_a_read_command_refuses_an_archive_owned_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    with bootstrap.archive_stores(root):
        pass
    monkeypatch.setattr(bootstrap, "_this_host", lambda: BETA)
    bootstrap._owner_checked.clear()
    with pytest.raises(OwnedElsewhere), bootstrap.query_index(root):
        pass
    with pytest.raises(OwnedElsewhere), bootstrap.archive_stores(root):
        pass
    assert owner_rows(root) == [("test-machine", "test-host")]


def test_takeover_creates_no_snapshot(tmp_path: Path, ext_dir: Path) -> None:
    con = connect_lake(tmp_path, ext_dir)
    con.execute("INSERT INTO run (run) VALUES ('r1')")
    check_owner(con, *ALPHA, take_ownership=False)

    def snapshots() -> int:
        rows = con.execute(
            "SELECT count(*) FROM __ducklake_metadata_lake.ducklake_snapshot"
        ).fetchall()
        return rows[0][0]

    before = snapshots()
    check_owner(con, *BETA, take_ownership=True)
    con.close()
    con = connect_lake(tmp_path, ext_dir)
    try:
        assert snapshots() == before
    finally:
        con.close()
