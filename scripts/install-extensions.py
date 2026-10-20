# ABOUTME: Installs the DuckDB extensions slarc needs into its cache directory.
# ABOUTME: Run by `just install` so a machine set up while online works offline afterwards.
import os
from pathlib import Path

from slarc.adapters.lake import extension_directory, install_extensions

install_extensions(extension_directory(os.environ, Path.home()))
