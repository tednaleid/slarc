# ABOUTME: Shared pytest configuration for slarc's tests.
# ABOUTME: Adds --update-golden, which rewrites golden files instead of comparing against them.
import pytest


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
