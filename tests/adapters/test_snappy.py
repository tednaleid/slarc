# ABOUTME: Tests for the Snappy block decompressor.
# ABOUTME: Uses hand-built streams covering long literals and all three copy encodings.
from slarc.adapters.snappy import snappy_decompress


def test_snappy_literal_only():
    assert snappy_decompress(b"\x05" + bytes([4 << 2]) + b"hello") == b"hello"

    long = bytes(i % 251 for i in range(300))
    stream = b"\xac\x02" + bytes([61 << 2]) + (299).to_bytes(2, "little") + long
    assert snappy_decompress(stream) == long


def test_snappy_copy_ops():
    stream = (
        bytes([21])
        + bytes([7 << 2])
        + b"abcdefgh"
        # 1-byte offset: length 4, offset 8
        + bytes([0x01, 0x08])
        # 2-byte offset: length 6, offset 2, overlapping its own output
        + bytes([(5 << 2) | 2])
        + (2).to_bytes(2, "little")
        # 4-byte offset: length 3, offset 18
        + bytes([(2 << 2) | 3])
        + (18).to_bytes(4, "little")
    )
    assert snappy_decompress(stream) == b"abcdefghabcdcdcdcdabc"


def test_snappy_one_byte_copy_uses_high_offset_bits():
    literal = bytes(i % 251 for i in range(300))
    # 1-byte offset copy: length 5, offset 300 (high bits 1 in the tag, low byte 44)
    stream = b"\xb1\x02" + bytes([61 << 2]) + (299).to_bytes(2, "little") + literal
    stream += bytes([(1 << 5) | (1 << 2) | 1, 300 & 0xFF])
    assert snappy_decompress(stream) == literal + literal[:5]
