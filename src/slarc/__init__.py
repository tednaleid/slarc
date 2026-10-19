# ABOUTME: slarc package root; exposes the installed version.
# ABOUTME: slarc archives Slack conversations into append-only JSONL files.
from importlib.metadata import version

__version__ = version("slarc")
