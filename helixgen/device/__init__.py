"""Device-side support for talking to Helix family hardware.

The modules here bridge this project's *host-side* view of a preset — models and
parameters addressed by name, as ``helix_model_information.json`` and ``.hlx``
files spell them — onto the *device-side* view, where both are addressed by
ordinal.

Nothing in this package ships Line 6 data. ``Helix.sym``, the table that supplies
the ordering, is read from the user's own HX Edit installation at runtime; see
``helixgen.device.symbols``.
"""

from .resolve import (
    AuditReport,
    ResolutionError,
    ResolvedModel,
    audit_catalog,
    resolve_model,
)
from .symbols import DeviceSymbols, SymbolsError

# helixgen.device.usb / helixgen.device.commands are deliberately NOT imported
# here. They pull in msgpack and pyusb, which are only needed for real USB
# device I/O (the CLI's devices/pull/backup/push commands, and helixgen_ui) -
# everything else must keep working with neither installed. Import them
# directly, e.g. `from helixgen.device.usb import find_devices, Session`.

__all__ = [
    "AuditReport",
    "DeviceSymbols",
    "ResolutionError",
    "ResolvedModel",
    "SymbolsError",
    "audit_catalog",
    "resolve_model",
]
