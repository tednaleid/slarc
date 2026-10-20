# ABOUTME: Shared pytest configuration and fixtures for slarc's tests; it adds --update-golden,
# ABOUTME: the shared DuckDB extension directory, a temporary lock cache and a fixed machine id.
import os
from pathlib import Path

import pytest

from slarc import bootstrap
from slarc.adapters.lake import extension_directory


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/ files from the current output",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture(scope="session")
def ext_dir() -> Path:
    """The real extension cache, so connecting downloads the DuckDB extensions at most once."""
    return extension_directory(os.environ, Path.home())


@pytest.fixture(autouse=True)
def lock_cache(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keeps the archive locks bootstrap takes out of the real cache directory."""
    cache = tmp_path_factory.getbasetemp() / "lock-cache"
    monkeypatch.setattr(bootstrap, "_cache_home", lambda: cache)


@pytest.fixture(autouse=True)
def this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gives bootstrap a fixed machine id, so no test reads the real one."""
    monkeypatch.setattr(bootstrap, "_this_host", lambda: ("test-machine", "test-host"))


@pytest.fixture(autouse=True)
def owner_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starts each test with no archive remembered as owner-checked."""
    monkeypatch.setattr(bootstrap, "_owner_checked", {})
