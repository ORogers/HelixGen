"""A small JSON file for the few things worth remembering between runs.

Deliberately not a settings system. It holds facts the program discovered and
should not have to discover again -- today, only where HX Edit is installed.
Anything a user tunes per run stays an argument.

Every read and write is forgiving: a missing, unreadable or corrupt file means
"nothing remembered", and a failed write is dropped. Losing a cached path costs
one rediscovery, which is never worth failing an upload over.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["config_path", "read_setting", "write_setting"]


def config_path() -> Path:
    """Where the config file lives, following each platform's convention."""
    override = os.environ.get("HLXGEN_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HelixPy" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "helixpy" / "config.json"


def _read_all() -> dict[str, Any]:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_setting(key: str) -> Any:
    return _read_all().get(key)


def write_setting(key: str, value: Any) -> None:
    """Set one key, or remove it when ``value`` is ``None``.

    Written through a temporary file in the same directory and renamed into
    place, so an interrupted write cannot leave a half-written config behind
    for the next run to choke on.
    """
    data = _read_all()
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
        temporary.replace(path)
    except OSError:
        return
