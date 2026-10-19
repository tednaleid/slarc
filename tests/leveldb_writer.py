# ABOUTME: Test helper that writes LevelDB .log and .ldb files for the reader to parse.
# ABOUTME: Checksums are written as zeros and Snappy blocks use literal-only encoding.
from pathlib import Path

LOG_BLOCK_SIZE = 32768
LOG_HEADER_SIZE = 7
FULL = 1
FIRST = 2
MIDDLE = 3
LAST = 4
PUT = 1
DELETE = 0
RESTART_INTERVAL = 16
TABLE_MAGIC = 0xDB4775248B80FB57
UNCOMPRESSED_BLOCK = 0
SNAPPY_BLOCK = 1
NO_CRC = b"\x00\x00\x00\x00"


def varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def length_prefixed(data: bytes) -> bytes:
    return varint(len(data)) + data


def write_log(path: Path, batches: list[list[tuple[bytes, bytes | None]]], start_seq: int) -> None:
    """Write each batch as one write batch; records get consecutive sequence numbers."""
    out = bytearray()
    seq = start_seq
    for batch in batches:
        payload = bytearray(seq.to_bytes(8, "little") + len(batch).to_bytes(4, "little"))
        for key, value in batch:
            if value is None:
                payload += bytes([DELETE]) + length_prefixed(key)
            else:
                payload += bytes([PUT]) + length_prefixed(key) + length_prefixed(value)
        _append_log_record(out, bytes(payload))
        seq += len(batch)
    path.write_bytes(out)


def _append_log_record(out: bytearray, payload: bytes) -> None:
    remaining = payload
    first = True
    while True:
        block_left = LOG_BLOCK_SIZE - len(out) % LOG_BLOCK_SIZE
        if block_left < LOG_HEADER_SIZE:
            out += bytes(block_left)
            block_left = LOG_BLOCK_SIZE
        fragment = remaining[: block_left - LOG_HEADER_SIZE]
        remaining = remaining[len(fragment) :]
        last = not remaining
        if first and last:
            kind = FULL
        elif first:
            kind = FIRST
        elif last:
            kind = LAST
        else:
            kind = MIDDLE
        out += NO_CRC + len(fragment).to_bytes(2, "little") + bytes([kind]) + fragment
        first = False
        if last:
            return


def write_table(
    path: Path,
    entries: list[tuple[bytes, int, bytes | None]],
    snappy: bool,
    block_size: int = 256,
) -> None:
    """Write (user key, seq, value) entries as a table; a value of None is a delete."""
    ordered = sorted(entries, key=lambda entry: (entry[0], -entry[1]))
    out = bytearray()
    index: list[tuple[bytes, bytes]] = []
    pending: list[tuple[bytes, bytes]] = []
    pending_size = 0
    for user_key, seq, value in ordered:
        kind = DELETE if value is None else PUT
        internal_key = user_key + ((seq << 8) | kind).to_bytes(8, "little")
        pending.append((internal_key, value or b""))
        pending_size += len(internal_key) + len(value or b"")
        if pending_size >= block_size:
            index.append((pending[-1][0], _append_block(out, pending, snappy)))
            pending = []
            pending_size = 0
    if pending:
        index.append((pending[-1][0], _append_block(out, pending, snappy)))

    metaindex_handle = _append_block(out, [], snappy)
    index_handle = _append_block(out, index, snappy)
    footer = (metaindex_handle + index_handle).ljust(40, b"\x00")
    out += footer + TABLE_MAGIC.to_bytes(8, "little")
    path.write_bytes(out)


def _append_block(out: bytearray, entries: list[tuple[bytes, bytes]], snappy: bool) -> bytes:
    """Append a block with its trailer and return its encoded block handle."""
    contents = _build_block(entries)
    if snappy:
        contents = _snappy_literal(contents)
    handle = varint(len(out)) + varint(len(contents))
    compression = SNAPPY_BLOCK if snappy else UNCOMPRESSED_BLOCK
    out += contents + bytes([compression]) + NO_CRC
    return handle


def _build_block(entries: list[tuple[bytes, bytes]]) -> bytes:
    out = bytearray()
    restarts: list[int] = []
    previous = b""
    for position, (key, value) in enumerate(entries):
        shared = 0
        if position % RESTART_INTERVAL == 0:
            restarts.append(len(out))
        else:
            while shared < min(len(previous), len(key)) and previous[shared] == key[shared]:
                shared += 1
        out += varint(shared) + varint(len(key) - shared) + varint(len(value))
        out += key[shared:] + value
        previous = key
    for restart in restarts or [0]:
        out += restart.to_bytes(4, "little")
    out += len(restarts or [0]).to_bytes(4, "little")
    return bytes(out)


def _snappy_literal(data: bytes) -> bytes:
    out = bytearray(varint(len(data)))
    for start in range(0, len(data), 65536):
        chunk = data[start : start + 65536]
        size = len(chunk) - 1
        if size < 60:
            out.append(size << 2)
        else:
            out.append(61 << 2)
            out += size.to_bytes(2, "little")
        out += chunk
    return bytes(out)
