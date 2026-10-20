# ABOUTME: Shared pytest configuration and fixtures for slarc's tests.
# ABOUTME: Adds --update-golden and the DuckDB extension directory the lake tests share.
import os
from pathlib import Path

import pytest

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
