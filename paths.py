"""XDG locations; no filesystem access or directory creation at import time."""
from __future__ import annotations

from pathlib import Path

from gi.repository import GLib


def config_dir() -> Path:
    return Path(GLib.get_user_config_dir()) / "simple-vpn"


def data_dir() -> Path:
    return Path(GLib.get_user_data_dir()) / "simple-vpn"


def downloads_dir() -> Path:
    directory = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    return Path(directory) if directory is not None else Path.home() / "Downloads"
