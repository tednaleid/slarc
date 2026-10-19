# ABOUTME: Read-only LevelDB reader over a database directory's .log and .ldb files.
# ABOUTME: Yields every put and delete record and resolves the newest value per key.
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from slarc.adapters.snappy import read_varint, snappy_decompress

LOG_BLOCK_SIZE = 32768
LOG_HEADER_SIZE = 7
FULL = 1
FIRST = 2
MIDDLE = 3
LAST = 4
BATCH_HEADER_SIZE = 12

PUT = 1

TABLE_MAGIC = 0xDB4775248B80FB57
FOOTER_SIZE = 48
UNCOMPRESSED_BLOCK = 0
SNAPPY_BLOCK = 1


@dataclass(frozen=True)
class LevelDbRecord:
    key: bytes
    value: bytes | None
    seq: int


def read_records(db_dir: Path) -> Iterator[LevelDbRecord]:
    for path in sorted(db_dir.iterdir()):
        if path.suffix == ".log":
            for batch in _log_payloads(path.read_bytes()):
                yield from _batch_records(batch)
        elif path.suffix == ".ldb":
            yield from _table_records(path.read_bytes())


def latest_values(db_dir: Path, key_filter: Callable[[bytes], bool]) -> dict[bytes, bytes]:
    newest: dict[bytes, LevelDbRecord] = {}
    for record in read_records(db_dir):
        if not key_filter(record.key):
            continue
        current = newest.get(record.key)
        if current is None or record.seq > current.seq:
            newest[record.key] = record
    return {key: record.value for key, record in newest.items() if record.value is not None}


def _log_payloads(data: bytes) -> Iterator[bytes]:
    """Reassemble the write batches stored in a log file, stopping at a truncated tail."""
    pos = 0
    pending: bytearray | None = None
    while pos + LOG_HEADER_SIZE <= len(data):
        block_left = LOG_BLOCK_SIZE - pos % LOG_BLOCK_SIZE
        if block_left < LOG_HEADER_SIZE:
            pos += block_left
            continue
        length = int.from_bytes(data[pos + 4 : pos + 6], "little")
        kind = data[pos + 6]
        start = pos + LOG_HEADER_SIZE
        end = start + length
        if end > len(data):
            return
        if kind not in (FULL, FIRST, MIDDLE, LAST):
            # Zeroed preallocation or an unknown record type: skip the rest of the block.
            pos += block_left
            pending = None
            continue
        fragment = data[start:end]
        pos = end
        if kind == FULL:
            pending = None
            yield fragment
        elif kind == FIRST:
            pending = bytearray(fragment)
        elif pending is not None:
            pending += fragment
            if kind == LAST:
                yield bytes(pending)
                pending = None


def _batch_records(batch: bytes) -> Iterator[LevelDbRecord]:
    seq = int.from_bytes(batch[0:8], "little")
    count = int.from_bytes(batch[8:BATCH_HEADER_SIZE], "little")
    pos = BATCH_HEADER_SIZE
    for index in range(count):
        kind = batch[pos]
        key, pos = _read_length_prefixed(batch, pos + 1)
        value = None
        if kind == PUT:
            value, pos = _read_length_prefixed(batch, pos)
        yield LevelDbRecord(key=key, value=value, seq=seq + index)


def _read_length_prefixed(data: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = read_varint(data, pos)
    return data[pos : pos + length], pos + length


def _table_records(data: bytes) -> Iterator[LevelDbRecord]:
    if len(data) < FOOTER_SIZE:
        return
    footer = data[-FOOTER_SIZE:]
    if int.from_bytes(footer[-8:], "little") != TABLE_MAGIC:
        return
    _metaindex_handle, pos = _read_block_handle(footer, 0)
    (index_offset, index_size), _ = _read_block_handle(footer, pos)
    index = _read_block(data, index_offset, index_size)
    for _last_key, handle in _block_entries(index):
        (offset, size), _ = _read_block_handle(handle, 0)
        for internal_key, value in _block_entries(_read_block(data, offset, size)):
            tag = int.from_bytes(internal_key[-8:], "little")
            yield LevelDbRecord(
                key=internal_key[:-8],
                value=value if tag & 0xFF == PUT else None,
                seq=tag >> 8,
            )


def _read_block_handle(data: bytes, pos: int) -> tuple[tuple[int, int], int]:
    offset, pos = read_varint(data, pos)
    size, pos = read_varint(data, pos)
    return (offset, size), pos


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    contents = data[offset : offset + size]
    compression = data[offset + size]
    if compression == SNAPPY_BLOCK:
        return snappy_decompress(contents)
    if compression == UNCOMPRESSED_BLOCK:
        return contents
    raise ValueError(f"unsupported LevelDB block compression type {compression}")


def _block_entries(block: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield (key, value) pairs, expanding keys that share a prefix with the previous key."""
    restart_count = int.from_bytes(block[-4:], "little")
    entries_end = len(block) - 4 - 4 * restart_count
    pos = 0
    key = b""
    while pos < entries_end:
        shared, pos = read_varint(block, pos)
        non_shared, pos = read_varint(block, pos)
        value_length, pos = read_varint(block, pos)
        key = key[:shared] + block[pos : pos + non_shared]
        pos += non_shared
        yield key, block[pos : pos + value_length]
        pos += value_length
