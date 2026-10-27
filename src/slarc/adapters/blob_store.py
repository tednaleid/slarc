# ABOUTME: BlobStore over the archive's content-addressed blob directory, blobs/sha256/ab/cd/<hash>.
# ABOUTME: Streams each write to a temp file while hashing, then renames it into place.
import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

TEMP_PREFIX = ".tmp-"


class FsBlobStore:
    def __init__(self, root: Path) -> None:
        self._dir = root / "blobs" / "sha256"

    def write(self, chunks: Iterable[bytes]) -> tuple[str, int]:
        self._dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(dir=self._dir, prefix=TEMP_PREFIX, delete=False) as tmp:
            try:
                for chunk in chunks:
                    tmp.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            except BaseException:
                tmp.close()
                os.unlink(tmp.name)
                raise
        sha256 = digest.hexdigest()
        path = self._path(sha256)
        if path.exists():
            os.unlink(tmp.name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp.name, path)
        return sha256, size

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()

    def read(self, sha256: str) -> bytes:
        return self._path(sha256).read_bytes()

    def ids(self) -> Iterator[str]:
        for path in self._dir.glob("*/*/*"):
            if path.is_file():
                yield path.name

    def delete(self, sha256: str) -> None:
        path = self._path(sha256)
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent):
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    def delete_partial(self) -> None:
        for path in self._dir.glob(f"{TEMP_PREFIX}*"):
            path.unlink(missing_ok=True)

    def read_start(self, sha256: str, size: int) -> bytes | None:
        """The blob's first size bytes, or None when the blob is missing."""
        try:
            with self._path(sha256).open("rb") as blob:
                return blob.read(size)
        except FileNotFoundError:
            return None

    def _path(self, sha256: str) -> Path:
        return self._dir / sha256[:2] / sha256[2:4] / sha256
