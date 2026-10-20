# ABOUTME: The archive's read and write locks: fcntl.flock on two files in the XDG cache directory,
# ABOUTME: named by the sha256 of the archive's real path, so the locks are per machine.
import fcntl
import hashlib
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class FlockArchiveLock:
    """ArchiveLock over `<cache_dir>/slarc/locks/<hash>.read` and `<hash>.write`.

    flock locks belong to an open file description, so two of these objects for one archive
    in one process block each other like two processes; use one object per archive per
    process. Releasing a lock closes its file, which is what releases the flock.
    """

    def __init__(self, root: Path, cache_dir: Path) -> None:
        name = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
        directory = cache_dir / "slarc" / "locks"
        self._read_path = directory / f"{name}.read"
        self._write_path = directory / f"{name}.write"
        self._shared = 0
        self._read_fd: int | None = None
        self._writing = False

    def hold_shared(self) -> None:
        # A second descriptor would wait on this process's own exclusive lock for ever.
        if self._shared == 0 and self._read_fd is not None:
            raise RuntimeError("Release the exclusive read lock before holding it shared")
        if self._shared == 0:
            self._read_fd = _locked(self._read_path, fcntl.LOCK_SH)
        self._shared += 1

    def release_shared(self) -> None:
        if self._shared == 0:
            raise RuntimeError("The read lock is not held shared")
        self._shared -= 1
        if self._shared == 0:
            self._close_read()

    def try_exclusive(self) -> bool:
        # Converting a held flock is not atomic: a failed try could lose the shared lock.
        if self._read_fd is not None:
            raise RuntimeError("Release the read lock before trying it exclusively")
        try:
            self._read_fd = _locked(self._read_path, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def release_exclusive(self) -> None:
        if self._shared or self._read_fd is None:
            raise RuntimeError("The read lock is not held exclusively")
        self._close_read()

    @contextmanager
    def write(self) -> Generator[None]:
        # A nested hold would wait on this process's own lock for ever.
        if self._writing:
            raise RuntimeError("The write lock is already held by this process")
        fd = _locked(self._write_path, fcntl.LOCK_EX)
        self._writing = True
        try:
            yield
        finally:
            self._writing = False
            os.close(fd)

    def _close_read(self) -> None:
        if self._read_fd is not None:
            os.close(self._read_fd)
            self._read_fd = None


def _locked(path: Path, operation: int) -> int:
    """Opens path, creating it and its directory, and flocks it; returns the descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, operation)
    except BaseException:
        os.close(fd)
        raise
    return fd
