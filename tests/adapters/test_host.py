# ABOUTME: Tests for reading this machine's id and host name for the archive's owner record.
# ABOUTME: ioreg and /etc/machine-id are faked; no test reads the real machine id.
import subprocess
from pathlib import Path

import pytest

from slarc.adapters import host
from slarc.adapters.host import NoMachineId, machine_id
from slarc.ports import SlarcError

IOREG_OUTPUT = """+-o J316sAP  <class IOPlatformExpertDevice, id 0x100000210, registered>
    {
      "IOPlatformSerialNumber" = "C02XXXXXXXXX"
      "IOPlatformUUID" = "4A2E1C9B-1111-2222-3333-444455556666"
      "model" = <"MacBookPro18,2">
    }
"""


def fake_ioreg(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0) -> list:
    calls = []

    def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(host.subprocess, "run", run)
    return calls


def test_machine_id_reads_the_platform_uuid_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_ioreg(monkeypatch, IOREG_OUTPUT)
    assert machine_id("darwin", tmp_path / "machine-id") == "4A2E1C9B-1111-2222-3333-444455556666"
    assert calls == [["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]]


def test_machine_id_reads_etc_machine_id_on_linux(tmp_path: Path) -> None:
    path = tmp_path / "machine-id"
    path.write_text("0f1e2d3c4b5a69788796a5b4c3d2e1f0\n")
    assert machine_id("linux", path) == "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_machine_id_without_a_source_is_a_slarc_error(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ioreg(monkeypatch, "", returncode=1)
    empty = tmp_path / "machine-id"
    empty.write_text("\n")
    with pytest.raises(NoMachineId) as raised:
        machine_id(platform, empty)
    assert isinstance(raised.value, SlarcError)
    assert str(raised.value) == (
        "slarc could not read this machine's id, which it needs to record the archive's owner."
    )
