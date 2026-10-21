# ABOUTME: Contract tests every BlobStore implementation must pass, over the fake and filesystem.
# ABOUTME: Covers content addressing, idempotent writes, listing, deleting and the on-disk path.
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from slarc.adapters.blob_store import FsBlobStore
from slarc.ports import BlobStore
from tests.fakes import InMemoryBlobStore


@pytest.fixture(params=["memory", "fs"])
def blobs(request, tmp_path: Path) -> BlobStore:
    if request.param == "memory":
        return InMemoryBlobStore()
    return FsBlobStore(tmp_path)


def test_blob_write_is_content_addressed(blobs: BlobStore):
    digest = hashlib.sha256(b"hello").hexdigest()
    assert not blobs.exists(digest)

    assert blobs.write([b"hel", b"lo"]) == (digest, 5)
    assert blobs.write(iter([b"hello"])) == (digest, 5)

    assert blobs.exists(digest)
    assert blobs.read(digest) == b"hello"


def test_fs_blob_path_and_no_leftover_temp_files(tmp_path: Path):
    digest, _ = FsBlobStore(tmp_path).write([b"hello"])
    FsBlobStore(tmp_path).write([b"hello"])

    path = tmp_path / "blobs" / "sha256" / digest[:2] / digest[2:4] / digest
    assert path.read_bytes() == b"hello"
    files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert files == [path]


def test_fs_delete_partial_removes_what_a_killed_writer_left(tmp_path: Path):
    blobs = FsBlobStore(tmp_path)
    digest, _ = blobs.write([b"whole"])
    partial = tmp_path / "blobs" / "sha256" / ".tmp-killed"
    partial.write_bytes(b"half")

    blobs.delete_partial()

    assert not partial.exists()
    assert list(blobs.ids()) == [digest]


def test_delete_partial_keeps_every_blob(blobs: BlobStore):
    digest, _ = blobs.write([b"whole"])

    blobs.delete_partial()

    assert list(blobs.ids()) == [digest]


def test_fs_concurrent_writes_of_the_same_content_leave_one_blob(tmp_path: Path):
    blobs = FsBlobStore(tmp_path)
    data = b"same bytes " * 10_000

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: blobs.write([data[:5000], data[5000:]]), range(16)))

    digest = hashlib.sha256(data).hexdigest()
    assert set(results) == {(digest, len(data))}
    files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert files == [tmp_path / "blobs" / "sha256" / digest[:2] / digest[2:4] / digest]


def test_blob_ids_and_delete(blobs: BlobStore):
    hello, _ = blobs.write([b"hello"])
    world, _ = blobs.write([b"world"])
    assert sorted(blobs.ids()) == sorted([hello, world])

    blobs.delete(hello)
    blobs.delete(hello)

    assert list(blobs.ids()) == [world]
    assert not blobs.exists(hello)
    assert blobs.read(world) == b"world"


def test_fs_blob_delete_removes_emptied_directories(tmp_path: Path):
    blobs = FsBlobStore(tmp_path)
    digest, _ = blobs.write([b"hello"])

    blobs.delete(digest)

    assert list((tmp_path / "blobs" / "sha256").iterdir()) == []
