#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "ccl_chromium_reader @ git+https://github.com/cclgroupltd/ccl_chromium_reader.git",
#     "slarc",
# ]
#
# [tool.uv.sources]
# slarc = { path = "..", editable = true }
# ///
# ABOUTME: Developer check that slarc's LevelDB reader finds the same tokens as ccl_chromium_reader.
# ABOUTME: Reads a copy of the Slack desktop app's Local Storage; prints only match or mismatch.
import json
import os
import sys
import tempfile
from pathlib import Path

from ccl_chromium_reader import ccl_chromium_localstorage

from slarc.adapters.chromium import read_local_config
from slarc.adapters.desktop_session import LEVELDB_SUBDIR, copy_leveldb, find_data_dir

LOCAL_CONFIG_KEY = "localConfig_v2"


def slarc_tokens(leveldb_dir: Path) -> dict[str, set[str]]:
    tokens: dict[str, set[str]] = {}
    for team in read_local_config(leveldb_dir):
        tokens.setdefault(team.team_id, set()).add(team.token)
    return tokens


def ccl_tokens(leveldb_dir: Path) -> dict[str, set[str]]:
    newest = {}
    db = ccl_chromium_localstorage.LocalStoreDb(leveldb_dir)
    try:
        for record in db.iter_all_records(include_deletions=True):
            if record.script_key != LOCAL_CONFIG_KEY:
                continue
            current = newest.get(record.storage_key)
            if current is None or record.leveldb_seq_number > current.leveldb_seq_number:
                newest[record.storage_key] = record
    finally:
        db.close()
    tokens: dict[str, set[str]] = {}
    for record in newest.values():
        if not record.is_live:
            continue
        for team_id, team in json.loads(record.value).get("teams", {}).items():
            tokens.setdefault(team_id, set()).add(team["token"])
    return tokens


def main() -> int:
    data_dir = find_data_dir(Path.home(), sys.platform, os.environ)
    if data_dir is None:
        print("no Slack desktop app data found")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "leveldb"
        copy_leveldb(data_dir / LEVELDB_SUBDIR, copy)
        ours = slarc_tokens(copy)
        theirs = ccl_tokens(copy)
    differing = sorted(t for t in ours.keys() | theirs.keys() if ours.get(t) != theirs.get(t))
    if differing:
        print(f"mismatch: {' '.join(differing)}")
        return 1
    print("match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
