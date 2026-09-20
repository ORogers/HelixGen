"""Finding the user's HX Edit installation.

Two files inside HX Edit are needed to talk to the pedal: ``Helix.sym``, the
symbol table that turns a model name into the ordinal the wire uses, and
``amp.models``, which says which cab an amp brings with it. Both are Line 6's
and neither is distributed here, so every device operation depends on locating
the app the user installed themselves. ``Helix.sym`` is not optional -- without
it a preset cannot be addressed at all.

Discovery used to be two literal ``/Applications`` paths, duplicated in two
modules. Anyone who keeps applications elsewhere -- ``~/Applications``, an
external volume, a folder of their own -- got a dead upload and, in the desktop
app, an unexplained failure. The search below keeps those two paths as the fast
common case and adds the ways out of it.

Nothing is ever copied out of the bundle: only the path to it is remembered.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from helixgen.config import read_setting, write_setting

__all__ = [
    "HX_EDIT_BUNDLE_ID",
    "HxEditNotFound",
    "amp_models_path",
    "find_hx_edit",
    "find_symbol_table",
    "forget_hx_edit",
    "remember_hx_edit",
    "resource_in_hx_edit",
    "symbol_table_path",
]

#: HX Edit's bundle identifier, used for the Spotlight lookup.
HX_EDIT_BUNDLE_ID = "com.line6.hxedit"

#: Overrides discovery entirely. Point it at the ``.app`` bundle.
HX_EDIT_ENV_VAR = "HELIXGEN_HX_EDIT"

#: Where the config file remembers a resolved or user-chosen install.
_CONFIG_KEY = "hx_edit_path"

#: The installer has used both of these, so both are tried.
KNOWN_BUNDLE_PATHS: tuple[Path, ...] = (
    Path("/Applications/Line6/HX Edit.app"),
    Path("/Applications/HX Edit.app"),
)

#: Resources read out of the bundle, relative to it.
_RESOURCES = "Contents/Resources"

#: Spotlight can be slow or disabled; a stalled query must not stall an upload.
_MDFIND_TIMEOUT_SECONDS = 5.0


def _is_hx_edit(bundle: Path) -> bool:
    """Whether a path looks like an HX Edit bundle we can read from.

    Checked by the file we actually need rather than by name, so a renamed or
    half-installed copy is rejected here instead of failing later with a
    confusing error about a missing symbol table.
    """
    return (bundle / _RESOURCES / "Helix.sym").is_file()


def _from_env() -> Path | None:
    raw = os.environ.get(HX_EDIT_ENV_VAR)
    if not raw:
        return None
    bundle = Path(raw).expanduser()
    return bundle if _is_hx_edit(bundle) else None


def _remembered() -> Path | None:
    """The path recorded by a previous discovery, if it still holds.

    Re-validated every time: an install that moved or was deleted must fall
    through to a fresh search rather than pinning the app to a dead path.
    """
    raw = read_setting(_CONFIG_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    bundle = Path(raw).expanduser()
    if _is_hx_edit(bundle):
        return bundle
    forget_hx_edit()
    return None


def _from_spotlight() -> Path | None:
    """Ask Launch Services, via Spotlight, where the bundle id is installed.

    This is what finds an install the known paths miss. Spotlight can be
    disabled, indexing an external volume can be stale, and ``mdfind`` does not
    exist off macOS, so every failure here means "not found" rather than an
    error.
    """
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{HX_EDIT_BUNDLE_ID}'"],
            capture_output=True,
            text=True,
            timeout=_MDFIND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        bundle = Path(line.strip())
        if line.strip() and _is_hx_edit(bundle):
            return bundle
    return None


def find_hx_edit(explicit: Path | None = None) -> Path | None:
    """The HX Edit bundle to read Line 6's data files from, or ``None``.

    First hit wins: an explicit path, the ``HELIXGEN_HX_EDIT`` environment
    variable, a previously remembered install, the two standard locations, then
    Spotlight. A successful search is remembered so the slow paths run once.
    """
    if explicit is not None:
        bundle = Path(explicit).expanduser()
        return bundle if _is_hx_edit(bundle) else None

    found = _from_env()
    if found is not None:
        return found

    found = _remembered()
    if found is not None:
        return found

    for candidate in KNOWN_BUNDLE_PATHS:
        if _is_hx_edit(candidate):
            remember_hx_edit(candidate)
            return candidate

    found = _from_spotlight()
    if found is not None:
        remember_hx_edit(found)
    return found


def remember_hx_edit(bundle: Path) -> None:
    """Record an install so later runs skip the search."""
    write_setting(_CONFIG_KEY, str(bundle))


def forget_hx_edit() -> None:
    write_setting(_CONFIG_KEY, None)


def resource_in_hx_edit(name: str, *, bundle: Path | None = None) -> Path | None:
    """One file inside HX Edit's ``Resources``, or ``None`` if it isn't there."""
    found = bundle if bundle is not None else find_hx_edit()
    if found is None:
        return None
    path = found / _RESOURCES / name
    return path if path.is_file() else None


def symbol_table_path(bundle: Path | None = None) -> Path | None:
    """``Helix.sym`` -- required for every device operation."""
    return resource_in_hx_edit("Helix.sym", bundle=bundle)


def amp_models_path(bundle: Path | None = None) -> Path | None:
    """``amp.models`` -- optional; without it an amp keeps a separate cab."""
    return resource_in_hx_edit("amp.models", bundle=bundle)


class HxEditNotFound(FileNotFoundError):
    """Raised when ``Helix.sym`` cannot be located.

    A ``FileNotFoundError`` so existing handlers keep working, with a message
    that says what to install and how to point at it.
    """


#: What to tell someone who has no reachable HX Edit install.
NOT_FOUND_MESSAGE = (
    "Could not find HX Edit. Helix.sym ships inside it and is not distributed "
    "with this project, and every device operation needs it.\n"
    "  - Install HX Edit (a free download from Line 6), or\n"
    f"  - set {HX_EDIT_ENV_VAR} to your copy of HX Edit.app, or\n"
    "  - pass --symbols with the path to Helix.sym:\n"
    '        find /Applications -name Helix.sym'
)


def find_symbol_table(explicit: Path | None = None) -> Path:
    """Locate ``Helix.sym``, which is not distributed with this project.

    ``explicit`` is the path to the symbol table itself, as ``--symbols`` takes
    it, not to the bundle.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise HxEditNotFound(f"No symbol table at {path}")
        return path
    found = symbol_table_path()
    if found is None:
        raise HxEditNotFound(NOT_FOUND_MESSAGE)
    return found
