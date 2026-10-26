# ABOUTME: Fixtures the export tests share: a recorded lake, a store over it and its blobs.
# ABOUTME: A test module that defines a fixture of the same name uses its own.
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.lake_store import DuckLakeArchiveStore
from tests.fakes import FakeArchiveLock, InMemoryBlobStore
from tests.recorded_fixtures import recorded_blobs, recorded_lake


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return recorded_lake(tmp_path / "archive")


@pytest.fixture
def store(archive: Path, ext_dir: Path) -> Iterator[DuckLakeArchiveStore]:
    store = DuckLakeArchiveStore(archive, ext_dir, FakeArchiveLock())
    yield store
    store.close()


@pytest.fixture(scope="module")
def blobs() -> InMemoryBlobStore:
    return recorded_blobs()
