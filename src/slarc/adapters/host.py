# ABOUTME: Identifies this machine for the archive's owner record: a stable machine id, read with
# ABOUTME: gethostuuid on macOS or from /etc/machine-id on Linux, and the host name for messages.
import ctypes
import socket
import sys
import uuid
from functools import cache
from pathlib import Path

from slarc.ports import SlarcError

LINUX_MACHINE_ID = Path("/etc/machine-id")


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
