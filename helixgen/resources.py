"""Where the data files live, wherever helixgen is running from.

The defaults used to be bare relative paths -- ``Path("helix_model_information.json")``
-- which resolve against the working directory. That works only in a git clone.
Installed as a wheel, run through ``pipx``, or double-clicked as a macOS app whose
working directory is ``/``, every one of them misses, and the first thing the user
sees is a missing-dataset error.

The files now ship inside :mod:`helixgen.data`, and everything that needs one asks
here. Command-line overrides (``--dataset``, ``--schema``, ``--template``) are
unaffected: these are only the defaults they fall back to.
"""

from __future__ import annotations

import sys
from importlib.resources import as_file, files
from pathlib import Path

__all__ = [
    "dataset_path",
    "default_output_dir",
    "is_frozen",
    "schema_path",
    "template_path",
    "upload_script_path",
]


def _data_file(name: str) -> Path:
    """Filesystem path to one bundled data file.

    ``as_file`` is the documented way to get a real path out of a package
    resource; for a normal directory install -- and for a PyInstaller bundle,
    which unpacks to a directory -- it hands back the file in place rather than
    a copy, so the returned path stays valid after the context exits. A
    zipimported install would not, but nothing here is importable from a zip:
    the dataset alone is 1.2 MB and is read as a file.
    """
    with as_file(files("helixgen.data").joinpath(name)) as path:
        return Path(path)


def dataset_path() -> Path:
    """The model catalog: which models exist and what each parameter accepts."""
    return _data_file("helix_model_information.json")


def schema_path() -> Path:
    """The JSON schema every preset is checked against before it is written."""
    return _data_file("helix-preset.schema.json")


def template_path() -> Path:
    """The known-good HX Stomp export generated presets are deep-copied from."""
    return _data_file("HXTemplate.hlx")


def upload_script_path() -> Path:
    """The AppleScript that drives HX Edit's UI, for the fallback upload path."""
    return _data_file("import_helix_preset.applescript")


def is_frozen() -> bool:
    """True inside a PyInstaller (or similar) application bundle."""
    return getattr(sys, "frozen", False)


def default_output_dir() -> Path:
    """Where a preset goes when no ``--output`` was given.

    A CLI should write next to where it was run from, so this stays relative to
    the working directory. A bundled app has no meaningful working directory --
    macOS launches it at ``/`` -- so it writes somewhere the user can find it.
    """
    if is_frozen():
        return Path.home() / "Documents" / "HelixGen" / "presets"
    return Path.cwd() / "generated-presets"
