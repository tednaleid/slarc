# ABOUTME: Decompresses raw Snappy blocks, as used inside LevelDB table files.
# ABOUTME: Also provides the unsigned little-endian base-128 varint reader both formats share.

LITERAL = 0
COPY_1_BYTE_OFFSET = 1
COPY_2_BYTE_OFFSET = 2


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Return the varint starting at `pos` and the position just past it."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def snappy_decompress(data: bytes) -> bytes:
    expected_length, pos = read_varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        element = tag & 0x03
        if element == LITERAL:
            length = tag >> 2
            if length >= 60:
                width = length - 59
                length = int.from_bytes(data[pos : pos + width], "little")
                pos += width
            length += 1
            if pos + length > len(data):
                raise ValueError("truncated snappy literal")
            out += data[pos : pos + length]
            pos += length
            continue

        if element == COPY_1_BYTE_OFFSET:
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
        elif element == COPY_2_BYTE_OFFSET:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 2], "little")
            pos += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
        if offset == 0 or offset > len(out):
            raise ValueError("snappy copy offset out of range")
        start = len(out) - offset
        if offset >= length:
            out += out[start : start + length]
        else:
            # The copy overlaps its own output, which repeats the last `offset` bytes.
            pattern = out[start:]
            out += (pattern * (length // offset + 1))[:length]

    if len(out) != expected_length:
        raise ValueError("snappy output length does not match its header")
    return bytes(out)
