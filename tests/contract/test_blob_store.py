# ABOUTME: Contract tests every BlobStore implementation must pass, over the fake and filesystem.
# ABOUTME: Covers content addressing, idempotent writes and the on-disk blob path.
import hashlib
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
