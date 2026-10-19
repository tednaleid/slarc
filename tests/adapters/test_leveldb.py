# ABOUTME: Tests for the read-only LevelDB reader over .log and .ldb files.
# ABOUTME: Builds databases with the test writer and checks records and latest values.
from pathlib import Path

from slarc.adapters.leveldb import LevelDbRecord, latest_values, read_records
from tests.leveldb_writer import write_log, write_table


def everything(_key: bytes) -> bool:
    return True


def test_log_single_put(tmp_path: Path):
    write_log(tmp_path / "000003.log", [[(b"key", b"value")]], start_seq=5)

    assert list(read_records(tmp_path)) == [LevelDbRecord(key=b"key", value=b"value", seq=5)]


def test_log_record_spanning_blocks(tmp_path: Path):
    big = bytes(i % 256 for i in range(70_000))
    log = tmp_path / "000003.log"
    write_log(log, [[(b"before", b"1")], [(b"big", big)], [(b"after", b"2")]], start_seq=1)

    assert log.stat().st_size > 2 * 32768
    assert latest_values(tmp_path, everything) == {b"before": b"1", b"big": big, b"after": b"2"}


def test_log_delete_hides_key(tmp_path: Path):
    write_log(
        tmp_path / "000003.log",
        [[(b"a", b"1"), (b"b", b"2")], [(b"a", None)]],
        start_seq=10,
    )

    assert list(read_records(tmp_path)) == [
        LevelDbRecord(key=b"a", value=b"1", seq=10),
        LevelDbRecord(key=b"b", value=b"2", seq=11),
        LevelDbRecord(key=b"a", value=None, seq=12),
    ]
    assert latest_values(tmp_path, everything) == {b"b": b"2"}


def test_log_truncated_tail_is_ignored(tmp_path: Path):
    log = tmp_path / "000003.log"
    write_log(log, [[(b"kept", b"1")], [(b"partial", b"x" * 100)]], start_seq=1)
    log.write_bytes(log.read_bytes()[:-50])

    assert latest_values(tmp_path, everything) == {b"kept": b"1"}


def table_entries(count: int) -> list[tuple[bytes, int, bytes | None]]:
    entries: list[tuple[bytes, int, bytes | None]] = [
        (f"key{i:03d}".encode(), 100 + i, f"value{i}".encode() * 3) for i in range(count)
    ]
    entries[7] = (entries[7][0], entries[7][1], None)
    return entries


def as_records(entries: list[tuple[bytes, int, bytes | None]]) -> list[LevelDbRecord]:
    return [LevelDbRecord(key=key, value=value, seq=seq) for key, seq, value in entries]


def test_table_uncompressed(tmp_path: Path):
    entries = table_entries(60)
    write_table(tmp_path / "000005.ldb", entries, snappy=False)

    assert list(read_records(tmp_path)) == as_records(entries)


def test_table_snappy_blocks(tmp_path: Path):
    entries = table_entries(60)
    plain_dir = tmp_path / "plain"
    snappy_dir = tmp_path / "snappy"
    plain_dir.mkdir()
    snappy_dir.mkdir()
    write_table(plain_dir / "000005.ldb", entries, snappy=False)
    write_table(snappy_dir / "000005.ldb", entries, snappy=True)

    plain_bytes = (plain_dir / "000005.ldb").read_bytes()
    assert (snappy_dir / "000005.ldb").read_bytes() != plain_bytes
    assert list(read_records(snappy_dir)) == as_records(entries)


def test_table_prefix_compression_across_restarts(tmp_path: Path):
    prefix = b"_https://app.slack.com\x00\x01"
    entries: list[tuple[bytes, int, bytes | None]] = [
        (prefix + f"setting{i:03d}".encode(), i + 1, b"v") for i in range(50)
    ]
    table = tmp_path / "000005.ldb"
    write_table(table, entries, snappy=False, block_size=1 << 16)

    assert table.stat().st_size < sum(len(key) for key, _, _ in entries)
    assert list(read_records(tmp_path)) == as_records(entries)


def test_highest_seq_wins_across_log_and_table(tmp_path: Path):
    write_table(tmp_path / "000099.ldb", [(b"token", 1, b"stale")], snappy=True)
    write_table(
        tmp_path / "000010.ldb",
        [(b"token", 20, b"older"), (b"other", 21, b"skipped")],
        snappy=False,
    )
    write_log(tmp_path / "000001.log", [[(b"token", b"newest")]], start_seq=30)

    assert latest_values(tmp_path, lambda key: key == b"token") == {b"token": b"newest"}


def test_ignores_lock_and_manifest_files(tmp_path: Path):
    for name in ["CURRENT", "LOCK", "LOG", "LOG.old", "MANIFEST-000001"]:
        (tmp_path / name).write_bytes(b"\xff\x00not leveldb data\x01" * 10)
    write_log(tmp_path / "000003.log", [[(b"key", b"value")]], start_seq=1)

    assert latest_values(tmp_path, everything) == {b"key": b"value"}
