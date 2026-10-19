# ABOUTME: Composition root: builds the real adapters the CLI commands use as one Services value.
# ABOUTME: Tests swap in fakes with override(), which current_services() then returns.
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import keyring

from slarc.adapters.credentials import CompositeSource, EnvSource, KeyringSource
from slarc.adapters.desktop_session import DesktopSessionSource
from slarc.adapters.slack_gateway import SessionConnector, session_gateway
from slarc.ports import Session, SlackGateway


@dataclass(frozen=True)
class Services:
    credentials: CompositeSource
    keychain: KeyringSource
    gateway_factory: Callable[[Session], SlackGateway]

    def connector(self) -> SessionConnector:
        return SessionConnector(self.credentials, self.gateway_factory)


def real_services() -> Services:
    keyring_source = KeyringSource(keyring)
    desktop = DesktopSessionSource(Path.home(), sys.platform)
    return Services(
        credentials=CompositeSource([EnvSource(os.environ), keyring_source, desktop]),
        keychain=keyring_source,
        gateway_factory=session_gateway,
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
