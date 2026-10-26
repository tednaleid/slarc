# ABOUTME: ExportWriters: files, creating their directories, or standard output for one file.
# ABOUTME: A relative path resolves against the current directory; failures become SlarcError.
import sys
from pathlib import Path

from slarc.ports import SlarcError


class FsExportWriter:
    def write(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as error:
            raise SlarcError(f"Could not write {path}: {error.strerror or error}.") from None


class StdoutExportWriter:
    """Writes a one-file export to standard output; the path names nothing."""

    def write(self, path: Path, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
