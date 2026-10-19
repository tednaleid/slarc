# ABOUTME: ExportWriter over the filesystem: writes each export file, creating its directories.
# ABOUTME: A relative path resolves against the current directory; failures become SlarcError.
from pathlib import Path

from slarc.ports import SlarcError


class FsExportWriter:
    def write(self, path: Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as error:
            raise SlarcError(f"Could not write {path}: {error.strerror or error}.") from None
