"""Upload a written preset to HX Edit.

Thin wrapper over ``hlxgen.cli._upload_via_script`` - the only working upload
mechanism today (AppleScript-driven HX Edit automation, macOS only; see
``docs/application_flow.md`` §5 and ``docs/usb_upload_plan.md`` for why). No
USB write path exists yet, so this module offers nothing beyond what the CLI's
``describe --upload`` flag already does; it exists only to give the UI a
plain function to call instead of building argparse ``Namespace`` objects.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hlxgen.cli import DEFAULT_UPLOAD_SCRIPT
from hlxgen.cli import _upload_via_script as upload_via_script

__all__ = ["DEFAULT_UPLOAD_SCRIPT", "UploadNotSupportedError", "upload_preset"]


class UploadNotSupportedError(RuntimeError):
    """Raised on any platform other than macOS - matches the CLI's own check."""


def upload_preset(
    preset_path: Path,
    *,
    script_path: Path | None = None,
    mode: str = "auto",
) -> None:
    """Trigger the HX Edit AppleScript uploader for ``preset_path``.

    Raises :class:`UploadNotSupportedError` off-macOS, or whatever
    ``_upload_via_script`` itself raises (missing script, missing
    ``osascript``, a non-zero AppleScript exit) - the UI shows the exception
    text verbatim, the same "Upload failed: {exc}" message the CLI prints.
    """
    if sys.platform != "darwin":
        raise UploadNotSupportedError("Upload is only supported on macOS.")
    upload_via_script(script_path or DEFAULT_UPLOAD_SCRIPT, preset_path, mode)
