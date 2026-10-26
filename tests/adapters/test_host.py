# ABOUTME: Tests for reading this machine's id, host name and local time zone.
# ABOUTME: The host UUID call, /etc/machine-id and /etc/localtime are faked in temp paths.
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from slarc.adapters import host
from slarc.adapters.host import NoMachineId, local_zone, machine_id
from slarc.ports import SlarcError

HOST_UUID = uuid.UUID("4a2e1c9b-1111-2222-3333-444455556666")


def fake_host_uuid(monkeypatch: pytest.MonkeyPatch, value: uuid.UUID | None) -> None:
    monkeypatch.setattr(host, "_host_uuid", lambda: value)


def test_machine_id_reads_the_host_uuid_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_host_uuid(monkeypatch, HOST_UUID)
    assert machine_id("darwin", tmp_path / "machine-id") == "4A2E1C9B-1111-2222-3333-444455556666"


def test_machine_id_reads_etc_machine_id_on_linux(tmp_path: Path) -> None:
    path = tmp_path / "machine-id"
    path.write_text("0f1e2d3c4b5a69788796a5b4c3d2e1f0\n")
    assert machine_id("linux", path) == "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_machine_id_without_a_source_is_a_slarc_error(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_host_uuid(monkeypatch, None)
    empty = tmp_path / "machine-id"
    empty.write_text("\n")
    with pytest.raises(NoMachineId) as raised:
        machine_id(platform, empty)
    assert isinstance(raised.value, SlarcError)
    assert str(raised.value) == (
        "slarc could not read this machine's id, which it needs to record the archive's owner."
    )


def test_local_zone_reads_tz(tmp_path: Path) -> None:
    assert local_zone({"TZ": ":America/Chicago"}, tmp_path / "none") == ZoneInfo("America/Chicago")


def test_local_zone_follows_etc_localtime(tmp_path: Path) -> None:
    target = tmp_path / "usr/share/zoneinfo/Europe/Paris"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(target)
    assert local_zone({}, link) == ZoneInfo("Europe/Paris")
    assert local_zone({"TZ": "Not/AZone"}, link) == ZoneInfo("Europe/Paris")


def test_local_zone_falls_back_to_the_current_offset(tmp_path: Path) -> None:
    zone = local_zone({}, tmp_path / "none")
    assert zone.utcoffset(datetime.now()) == datetime.now().astimezone().utcoffset()
