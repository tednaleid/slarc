# ABOUTME: Facts about this machine: a stable machine id for the archive's owner record (gethostuuid
# ABOUTME: on macOS, /etc/machine-id on Linux), the host name for messages, and the local time zone.
import ctypes
import socket
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, tzinfo
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from slarc.ports import SlarcError

LINUX_MACHINE_ID = Path("/etc/machine-id")
LOCALTIME = Path("/etc/localtime")


class NoMachineId(SlarcError):
    def __init__(self) -> None:
        super().__init__(
            "slarc could not read this machine's id, which it needs to record the archive's owner."
        )


@cache
def machine_id(platform: str = sys.platform, linux_machine_id: Path = LINUX_MACHINE_ID) -> str:
    """The host UUID on macOS, which is the IOPlatformUUID, or the contents of /etc/machine-id
    elsewhere.

    Raises NoMachineId when neither yields an id.
    """
    if platform == "darwin":
        found = _host_uuid()
        if found is not None:
            return str(found).upper()
    else:
        try:
            text = linux_machine_id.read_text().strip()
        except OSError:
            text = ""
        if text:
            return text
    raise NoMachineId()


def host_name() -> str:
    return socket.gethostname()


def _host_uuid() -> uuid.UUID | None:
    """The UUID macOS's gethostuuid(2) returns, or None when the call fails."""
    try:
        libc = ctypes.CDLL(None)
    except OSError:
        return None
    buffer = (ctypes.c_ubyte * 16)()
    no_wait = (ctypes.c_long * 2)(0, 0)
    if libc.gethostuuid(buffer, no_wait) != 0:
        return None
    return uuid.UUID(bytes=bytes(buffer))


def local_zone(env: Mapping[str, str], localtime: Path = LOCALTIME) -> tzinfo:
    """This machine's time zone, with its daylight saving rules: the zone TZ names, else the
    one /etc/localtime links to, else a fixed zone at the current offset."""
    for name in (env.get("TZ", "").removeprefix(":"), _linked_zone(localtime)):
        if name:
            try:
                return ZoneInfo(name)
            except ZoneInfoNotFoundError, ValueError:
                continue
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def _linked_zone(localtime: Path) -> str:
    """The zone name in the path /etc/localtime links to, such as America/Chicago, or ""."""
    try:
        target = str(localtime.resolve(strict=True))
    except OSError:
        return ""
    _, found, name = target.partition("/zoneinfo/")
    return name if found else ""
