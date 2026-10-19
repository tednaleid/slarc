#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
# ABOUTME: Developer script that writes src/slarc/data/emoji.json, the standard emoji table,
# ABOUTME: from iamcal/emoji-data's emoji.json at a release tag: short name to unified code points.
import argparse
import json
import urllib.request
from pathlib import Path

TAG = "v16.0.0"
SOURCE = "https://raw.githubusercontent.com/iamcal/emoji-data/{tag}/emoji.json"
OUTPUT = Path(__file__).parent.parent / "src" / "slarc" / "data" / "emoji.json"
SKIN_TONES = {"1F3FB": "2", "1F3FC": "3", "1F3FD": "4", "1F3FE": "5", "1F3FF": "6"}


def table(source: list[dict]) -> dict[str, dict]:
    """{short_name: {"u": unified, "skins": {"3": unified, "2-4": unified}}}, keyed by every
    short name. Skin keys are Slack's skin-tone numbers, 2 to 6."""
    entries: dict[str, dict] = {}
    for emoji in source:
        entry: dict = {"u": emoji["unified"]}
        skins = {
            "-".join(SKIN_TONES[tone] for tone in key.split("-")): variation["unified"]
            for key, variation in (emoji.get("skin_variations") or {}).items()
        }
        if skins:
            entry["skins"] = dict(sorted(skins.items()))
        for name in emoji["short_names"]:
            entries[name] = entry
    return dict(sorted(entries.items()))


def encode(entries: dict[str, dict]) -> str:
    """Compact JSON with one entry per line, so a regenerated table diffs by emoji."""
    lines = [
        f"{json.dumps(name)}:{json.dumps(e, separators=(',', ':'))}" for name, e in entries.items()
    ]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=TAG, help=f"iamcal/emoji-data release tag (default {TAG})")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    with urllib.request.urlopen(SOURCE.format(tag=args.tag), timeout=60) as response:
        source = json.load(response)
    entries = table(source)
    args.output.write_text(encode(entries), encoding="utf-8")
    print(f"wrote {len(entries)} emoji from iamcal/emoji-data {args.tag} to {args.output}")


if __name__ == "__main__":
    main()
