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
from slarc.adapters.lake import CATALOG, connect_lake, extension_directory
from slarc.adapters.lake_store import DuckLakeArchiveStore
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


def ensure_lake(root: Path) -> None:
    """Creates an empty lake in the archive if it has no catalog."""
    if not (root / CATALOG).exists():
        connect_lake(root, _extension_directory()).close()


@contextmanager
def archive_stores(root: Path) -> Generator[tuple[DuckLakeArchiveStore, FsBlobStore]]:
    """The archive's lake and blob stores, with ensure_lake run first; closed on exit."""
    ensure_lake(root)
    archive = DuckLakeArchiveStore(root, _extension_directory())
    try:
        yield archive, FsBlobStore(root)
    finally:
        archive.close()


@contextmanager
def query_index(root: Path) -> Generator[QueryIndex]:
    """Views over the archive's lake, with ensure_lake run first; closed on exit."""
    ensure_lake(root)
    with DuckDbQueryIndex(root, _extension_directory()) as index:
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
