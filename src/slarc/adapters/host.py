# ABOUTME: Identifies this machine for the archive's owner record: a stable machine id, read with
# ABOUTME: ioreg on macOS or from /etc/machine-id on Linux, and the host name for messages.
import re
import socket
import subprocess
import sys
from pathlib import Path

from slarc.ports import SlarcError

IOREG = ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
PLATFORM_UUID = re.compile(r'"IOPlatformUUID" = "([^"]+)"')
LINUX_MACHINE_ID = Path("/etc/machine-id")


class NoMachineId(SlarcError):
    def __init__(self) -> None:
        super().__init__(
            "slarc could not read this machine's id, which it needs to record the archive's owner."
        )


def machine_id(platform: str = sys.platform, linux_machine_id: Path = LINUX_MACHINE_ID) -> str:
    """The IOPlatformUUID on macOS, or the contents of /etc/machine-id elsewhere.

    Raises NoMachineId when neither yields an id.
    """
    if platform == "darwin":
        found = PLATFORM_UUID.search(_ioreg())
        if found:
            return found.group(1)
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


def _ioreg() -> str:
    try:
        result = subprocess.run(IOREG, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""
