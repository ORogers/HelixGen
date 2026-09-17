"""Each amp's default cabinet, from HX Edit's ``amp.models``.

On the pedal an amp and its cab are normally **one block**, not two: the cab is
fused into the amp's slot as a *paired model*, which is why a preset with six
effects and an amp+cab still fits in a Stomp's eight slots. Selecting an amp in
HX Edit brings its own cab with it.

``amp.models`` is what says which cab that is. Every amp entry carries two:

``cablink``
    The **paired** cab, fused into the amp's slot. A simpler model than a
    standalone cab -- ``[Distance, LowCut, HighCut, EarlyReflections, Level]``,
    with no mic selection, position or angle, because the amp block owns them.

``ircablink``
    The default **standalone** IR cab, for a separate cab block.

The two are different models, not one model in two places, so pairing a cab is
not a lossless relocation of a standalone one: mic, position and angle have
nowhere to go, and ``EarlyReflections`` appears from nowhere. That is also why
only 20 of the 92 ``HD2_CabMicIr_*`` symbols have a paired counterpart at all --
preferring a tone's own cab over the amp's default fails for most cabs, which is
why the default is what gets used.

This file is Line 6's and is deliberately not vendored. It is read from the
user's own HX Edit installation, the same way ``Helix.sym`` is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Where HX Edit keeps the amp definitions on macOS.
AMP_MODEL_PATHS: Final = (
    Path("/Applications/Line6/HX Edit.app/Contents/Resources/amp.models"),
    Path("/Applications/HX Edit.app/Contents/Resources/amp.models"),
)


class AmpDataError(RuntimeError):
    """Raised when ``amp.models`` cannot be read or has an unexpected shape."""


@dataclass(frozen=True)
class AmpModel:
    """One amp and the cabs it defaults to."""

    symbol: str
    name: str
    #: The cab fused into the amp's own slot.
    paired_cab: str | None
    #: The cab used when a separate cab block is wanted.
    ir_cab: str | None


class AmpDefaults:
    """Amp symbol to default cabinet."""

    def __init__(self, amps: dict[str, AmpModel]):
        self._amps = amps

    @classmethod
    def parse(cls, payload: bytes | str) -> AmpDefaults:
        try:
            entries = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AmpDataError(f"amp.models is not valid JSON: {exc}") from exc
        if not isinstance(entries, list):
            raise AmpDataError("amp.models should be a JSON array of amp entries")

        amps: dict[str, AmpModel] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbolicID")
            if not isinstance(symbol, str):
                continue
            amps[symbol] = AmpModel(
                symbol=symbol,
                name=entry.get("name") or symbol,
                paired_cab=entry.get("cablink") or None,
                ir_cab=entry.get("ircablink") or None,
            )
        if not amps:
            raise AmpDataError("amp.models carried no amp entries")
        return cls(amps)

    @classmethod
    def load(cls, path: Path | None = None) -> AmpDefaults:
        """Read ``amp.models``, finding it in HX Edit if no path is given."""
        if path is not None:
            candidates = [Path(path)]
        else:
            candidates = [p for p in AMP_MODEL_PATHS if p.exists()]
        for candidate in candidates:
            if candidate.exists():
                return cls.parse(candidate.read_bytes())
        raise AmpDataError(
            "Could not find amp.models. It ships inside HX Edit and is not "
            "distributed with this project."
        )

    def __len__(self) -> int:
        return len(self._amps)

    def is_amp(self, symbol: str) -> bool:
        """Whether a device symbol is an amp that can carry a cab."""
        return symbol in self._amps

    def get(self, symbol: str) -> AmpModel | None:
        return self._amps.get(symbol)

    def paired_cab(self, symbol: str) -> str | None:
        """The cab an amp fuses into its own slot, or ``None``."""
        amp = self._amps.get(symbol)
        return amp.paired_cab if amp else None
