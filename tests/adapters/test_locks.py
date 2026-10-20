# ABOUTME: Tests for the archive's flock read and write locks, across processes and within one.
# ABOUTME: The cross-process cases run child processes with this venv's python.
import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from slarc import bootstrap
from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import check_owner, connect_lake
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.adapters.locks import FlockArchiveLock

COMMITS = 50

# Opens the store on a new archive, so both children also race to create the lake.
COMMITTER = """
import sys
from pathlib import Path

from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.adapters.locks import FlockArchiveLock
from slarc.domain.envelope import Envelope, Meta
from slarc.ports import Commit

root, cache, ext_dir, channel, count = sys.argv[1:]
root = Path(root)
store = DuckLakeArchiveStore(root, Path(ext_dir), FlockArchiveLock(root, Path(cache)))
for i in range(int(count)):
    store.append(Envelope(meta=Meta(kind="message", team="T1", channel=channel, ts=f"{i}.0")))
    store.commit(Commit())
store.close()
"""

HOLDER = """
import sys
from pathlib import Path

from slarc.adapters.locks import FlockArchiveLock

FlockArchiveLock(Path(sys.argv[1]), Path(sys.argv[2])).hold_shared()
print("held", flush=True)
sys.stdin.readline()
"""


def test_two_processes_commit_concurrently_without_errors(tmp_path: Path, ext_dir: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", COMMITTER, root, cache, ext_dir, channel, str(COMMITS)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for channel in ("C1", "C2")
    ]
    errors = [child.communicate(timeout=60)[1] for child in children]
    assert [child.returncode for child in children] == [0, 0], errors
    con = connect_lake(root, ext_dir, read_only=True)
    counts = con.execute(
        "SELECT channel, count(DISTINCT ts) FROM message_version GROUP BY channel ORDER BY channel"
    ).fetchall()
    con.close()
    assert counts == [("C1", COMMITS), ("C2", COMMITS)]


def test_try_exclusive_fails_while_another_process_holds_shared(tmp_path: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, root, cache],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdin is not None
    assert holder.stdout.readline() == "held\n"
    lock = FlockArchiveLock(root, cache)
    assert not lock.try_exclusive()
    holder.stdin.close()
    assert holder.wait(timeout=30) == 0
    assert lock.try_exclusive()
    lock.release_exclusive()


def test_one_lock_object_per_archive_per_process(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    link = tmp_path / "link"
    link.symlink_to(archive)
    lock = bootstrap.archive_lock(archive)
    assert bootstrap.archive_lock(link) is lock
    assert bootstrap.archive_lock(tmp_path / "other") is not lock


def test_lock_files_are_named_by_the_archive_real_path(tmp_path: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    lock.hold_shared()
    with lock.write():
        pass
    lock.release_shared()
    name = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    assert sorted(p.name for p in (cache / "slarc" / "locks").iterdir()) == [
        f"{name}.read",
        f"{name}.write",
    ]


def test_shared_hold_is_counted_per_process(tmp_path: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    # flock treats a second open file description like another process.
    other = FlockArchiveLock(root, cache)
    lock.hold_shared()
    lock.hold_shared()
    lock.release_shared()
    assert not other.try_exclusive()
    lock.release_shared()
    assert other.try_exclusive()
    other.release_exclusive()


def test_try_exclusive_refuses_while_this_process_holds_shared(tmp_path: Path) -> None:
    lock = FlockArchiveLock(tmp_path / "archive", tmp_path / "cache")
    lock.hold_shared()
    with pytest.raises(RuntimeError):
        lock.try_exclusive()
    lock.release_shared()


def test_store_and_index_hold_the_read_lock_while_open(tmp_path: Path, ext_dir: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    other = FlockArchiveLock(root, cache)
    store = DuckLakeArchiveStore(root, ext_dir, lock)
    index = DuckDbQueryIndex(root, ext_dir, lock)
    store.close()
    assert not other.try_exclusive()
    index.close()
    assert other.try_exclusive()
    other.release_exclusive()


def test_a_read_command_on_an_archive_this_machine_owns_takes_no_write_lock(
    tmp_path: Path, ext_dir: Path
) -> None:
    root = tmp_path / "archive"
    con = connect_lake(root, ext_dir)
    check_owner(con, *bootstrap._this_host(), take_ownership=False)
    con.close()
    writer = FlockArchiveLock(root, bootstrap._cache_home())
    opened = threading.Event()

    def read() -> None:
        with bootstrap.query_index(root):
            opened.set()

    with writer.write():
        reader = threading.Thread(target=read)
        reader.start()
        held = opened.wait(timeout=10)
    reader.join()
    assert held


def test_hold_shared_refuses_while_this_process_holds_exclusive(tmp_path: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    assert lock.try_exclusive()
    with pytest.raises(RuntimeError):
        lock.hold_shared()
    lock.release_exclusive()
    other = FlockArchiveLock(root, cache)
    assert other.try_exclusive()
    other.release_exclusive()


def test_nested_write_raises_instead_of_waiting_for_itself(tmp_path: Path) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    with lock.write(), pytest.raises(RuntimeError), lock.write():
        pass
    other = FlockArchiveLock(root, cache)
    with other.write():
        pass
