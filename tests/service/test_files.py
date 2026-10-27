# ABOUTME: Tests for reading one archived file's bytes by its id or Slack link, over the
# ABOUTME: recorded lake and blobs, including files the archive knows but never downloaded.
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.ports import SlarcError
from slarc.service.files import FileNotArchived, file_bytes
from tests.fakes import FakeArchiveLock, InMemoryBlobStore
from tests.recorded_fixtures import recorded_lake

TEN_K = "F0CA7TKM285"
TEN_K_BLOB = "72e62bcee33d30d5fffe5178c6b982bc9670abbbe1d99665dd572de094af20b4"
TEN_K_LINK = f"https://naleidworkspace.slack.com/files/U0CA538UEN7/{TEN_K}/10k-snippet.txt"
DELETED = "F0CAF2TQHQ9"


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory, ext_dir: Path) -> Iterator[DuckDbQueryIndex]:
    root = recorded_lake(tmp_path_factory.mktemp("recorded") / "archive")
    with DuckDbQueryIndex(root, ext_dir, FakeArchiveLock()) as idx:
        yield idx


def test_file_bytes_by_id_and_by_link(index: DuckDbQueryIndex, blobs: InMemoryBlobStore) -> None:
    for selector in (TEN_K, TEN_K_LINK):
        name, data = file_bytes(index, blobs, selector)
        assert name == "10k-snippet.txt"
        assert data == blobs.read(TEN_K_BLOB)


def test_file_not_downloaded_says_why(index: DuckDbQueryIndex, blobs: InMemoryBlobStore) -> None:
    with pytest.raises(FileNotArchived) as raised:
        file_bytes(index, blobs, DELETED)
    assert str(raised.value) == (
        f"{DELETED} is not in the archive: it was deleted from Slack before slarc saved it."
    )


def test_file_without_its_blob_is_not_archived(index: DuckDbQueryIndex) -> None:
    with pytest.raises(FileNotArchived, match=f"{TEN_K} is not in the archive"):
        file_bytes(index, InMemoryBlobStore(), TEN_K)


def test_unknown_or_unreadable_file_is_an_error(
    index: DuckDbQueryIndex, blobs: InMemoryBlobStore
) -> None:
    cases = {
        "F0NOPE00000": "No file F0NOPE00000 is in the archive.",
        "#general": "slarc file takes a file id such as F0123456789 or a file's Slack link, "
        "not '#general'.",
        f"https://other.slack.com/files/U1/{TEN_K}/x": "This archive holds no workspace other.",
    }
    for selector, message in cases.items():
        with pytest.raises(SlarcError) as raised:
            file_bytes(index, blobs, selector)
        assert str(raised.value).startswith(message), selector
