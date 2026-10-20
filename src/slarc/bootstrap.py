# ABOUTME: Composition root: builds the real adapters the CLI commands use as one Services value.
# ABOUTME: Tests swap in fakes with override(), which current_services() then returns.
import os
import secrets
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import keyring

from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.credentials import CompositeSource, EnvSource, KeyringSource
from slarc.adapters.desktop_session import DesktopSessionSource
from slarc.adapters.downloader import HttpDownloader
from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.export_writer import FsExportWriter
from slarc.adapters.host import host_name, machine_id
from slarc.adapters.lake import (
    CATALOG,
    cache_home,
    check_owner,
    connect_lake,
    extension_directory,
    read_owner,
)
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.adapters.locks import FlockArchiveLock
from slarc.adapters.slack_gateway import USER_AGENT, SessionConnector, session_gateway
from slarc.ports import (
    BlobStore,
    Clock,
    Downloader,
    ExportWriter,
    QueryIndex,
    Session,
    SlackGateway,
)

DOWNLOAD_TIMEOUT_S = 60.0

type DownloaderFactory = Callable[
    [BlobStore, dict[str, Session]], AbstractContextManager[Downloader]
]


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@contextmanager
def http_downloader(blobs: BlobStore, sessions: dict[str, Session]) -> Generator[Downloader]:
    """sessions maps a team id to its session and may gain entries while downloads run."""
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, timeout=DOWNLOAD_TIMEOUT_S) as client:
        yield HttpDownloader(blobs, sessions, client)


def _extension_directory() -> Path:
    return extension_directory(os.environ, Path.home())


def _cache_home() -> Path:
    return cache_home(os.environ, Path.home())


def _this_host() -> tuple[str, str]:
    return machine_id(), host_name()


_locks: dict[Path, FlockArchiveLock] = {}
_owner_checked: dict[
    tuple[Path, str], bool
] = {}  # (root, machine id) to whether the run is cautious


def archive_lock(root: Path) -> FlockArchiveLock:
    """The archive's lock, one object per archive per process so its flocks do not collide."""
    key = root.resolve()
    if key not in _locks:
        _locks[key] = FlockArchiveLock(key, _cache_home())
    return _locks[key]


def ensure_lake(root: Path) -> None:
    """Creates an empty lake in the archive if it has no catalog, under the archive's locks.

    connect_lake renames a new catalog into place complete, so finding one needs no lock.
    """
    if (root / CATALOG).exists():
        return
    lock = archive_lock(root)
    lock.hold_shared()
    try:
        with lock.write():
            if not (root / CATALOG).exists():
                connect_lake(root, _extension_directory()).close()
    finally:
        lock.release_shared()


def _check_owner(root: Path, take_ownership: bool) -> bool:
    """Checks the existing lake's owner record against this machine; returns whether the run
    is cautious. See check_owner.

    A record naming this machine is read under the read lock alone. Recording this machine, on
    the first open or when taking ownership, takes the write lock as well. A check that has
    passed for this archive and machine in this process is not repeated.
    """
    machine, host = _this_host()
    key = (root.resolve(), machine)
    if key in _owner_checked:
        return _owner_checked[key]
    cautious = _check_owner_once(root, machine, host, take_ownership)
    _owner_checked[key] = cautious
    return cautious


def _check_owner_once(root: Path, machine: str, host: str, take_ownership: bool) -> bool:
    lock = archive_lock(root)
    lock.hold_shared()
    try:
        if not take_ownership:
            con = connect_lake(root, _extension_directory(), read_only=True)
            try:
                if read_owner(con) is not None:
                    return check_owner(con, machine, host, take_ownership=False)
            finally:
                con.close()
        with lock.write():
            con = connect_lake(root, _extension_directory())
            try:
                return check_owner(con, machine, host, take_ownership)
            finally:
                con.close()
    finally:
        lock.release_shared()


@contextmanager
def archive_stores(
    root: Path, take_ownership: bool = False
) -> Generator[tuple[DuckLakeArchiveStore, FsBlobStore]]:
    """The archive's lake and blob stores, with ensure_lake and the owner check run first;
    closed on exit."""
    ensure_lake(root)
    cautious = _check_owner(root, take_ownership)
    archive = DuckLakeArchiveStore(
        root, _extension_directory(), archive_lock(root), cautious=cautious
    )
    try:
        yield archive, FsBlobStore(root)
    finally:
        archive.close()


@contextmanager
def query_index(root: Path, take_ownership: bool = False) -> Generator[QueryIndex]:
    """Views over the archive's lake, with ensure_lake and the owner check run first; closed on
    exit."""
    ensure_lake(root)
    _check_owner(root, take_ownership)
    with DuckDbQueryIndex(root, _extension_directory(), archive_lock(root)) as index:
        yield index


def export_writer() -> ExportWriter:
    return FsExportWriter()


@dataclass(frozen=True)
class Services:
    credentials: CompositeSource
    keychain: KeyringSource
    gateway_factory: Callable[[Session], SlackGateway]
    clock: Clock = field(default_factory=SystemClock)
    downloader: DownloaderFactory = http_downloader
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: Path = field(default_factory=Path.home)
    run_suffix: str = ""  # ends the run id, telling apart commands started in the same second
    take_ownership: bool = False  # the global --take-ownership option

    def connector(self) -> SessionConnector:
        return SessionConnector(self.credentials, self.gateway_factory)


def real_services() -> Services:
    keyring_source = KeyringSource(keyring)
    desktop = DesktopSessionSource(Path.home(), sys.platform)
    return Services(
        credentials=CompositeSource([EnvSource(os.environ), keyring_source, desktop]),
        keychain=keyring_source,
        gateway_factory=session_gateway,
        run_suffix=secrets.token_hex(2),
    )


_override: Services | None = None


def current_services() -> Services:
    return _override if _override is not None else real_services()


@contextmanager
def override(services: Services) -> Generator[None]:
    global _override
    previous, _override = _override, services
    try:
        yield
    finally:
        _override = previous
