# ABOUTME: Tests for the tracking structs that commits compare: download states cannot change
# ABOUTME: in place, so every change is a new value that UnitOfWork.commit sees.
import msgspec
import pytest

from slarc.domain.tracking import DownloadState


def test_a_download_state_cannot_change_in_place():
    state = DownloadState(team_id="T", target_kind="image", target="u", status="pending")

    with pytest.raises(AttributeError):
        state.status = "failed"  # ty: ignore[invalid-assignment]

    changed = msgspec.structs.replace(state, status="failed")
    assert (state.status, changed.status) == ("pending", "failed")
