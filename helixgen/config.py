"""A small JSON file for the things worth remembering between runs.

Deliberately not a settings system with layers and precedence. It holds what a
person set once and should not have to set again: where HX Edit was found, the
backend they chose, and their OpenAI key.

Every read is forgiving. A missing, unreadable or corrupt file means "nothing
remembered", because losing a cached path costs one rediscovery and that is
never worth failing an upload over.

**It can hold an API key**, so the file is created `rw-------` and every write
re-asserts that. A key on disk in plain text is still a key on disk: anything
running as this user can read it. That is the same exposure as the `.env` file
the CLI already reads, and the trade for an app a person can set up without
opening a terminal.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "config_path",
    "read_setting",
    "read_settings",
    "write_setting",
    "write_settings",
]

#: Owner read/write only. This file can contain an API key.
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def config_path() -> Path:
    """Where the config file lives, following each platform's convention."""
    override = os.environ.get("HELIXGEN_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HelixGen" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "helixgen" / "config.json"


def _legacy_config_path() -> Path:
    """Where the config lived when the project was called HelixPy.

    Read from, never written to. It can go once anyone who ran a pre-rename
    build has opened the app again - there was no release under the old name,
    so this is a courtesy to a handful of people rather than a migration.
    """
    override = os.environ.get("HELIXGEN_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HelixPy" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "helixpy" / "config.json"


def read_settings() -> dict[str, Any]:
    """Everything remembered, or an empty mapping.

    Falls back to the pre-rename location when there is nothing at the current
    one, so an API key set up before the rename is not silently lost. The first
    write lands in the new place.
    """
    for path in (config_path(), _legacy_config_path()):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def read_setting(key: str) -> Any:
    return read_settings().get(key)


def write_setting(key: str, value: Any) -> None:
    """Set one key, or remove it when ``value`` is ``None``."""
    write_settings({key: value})


def write_settings(values: dict[str, Any]) -> None:
    """Merge ``values`` in, removing any whose value is ``None``.

    Written to a temporary file in the same directory and renamed into place,
    so an interrupted write cannot leave a half-written config behind for the
    next run to choke on - and so the key never exists on disk under a
    world-readable mode, even briefly.
    """
    data = read_settings()
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value

    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        # NamedTemporaryFile is already 0600; set it explicitly so the mode is
        # a stated property of this file rather than a detail of tempfile.
        temporary.chmod(_FILE_MODE)
        temporary.replace(path)
    except OSError:
        return
