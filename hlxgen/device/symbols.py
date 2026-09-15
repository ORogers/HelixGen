"""Parser for ``Helix.sym`` — the device's authoritative model and parameter order.

``Helix.sym`` ships inside HX Edit and is a JSON array of objects shaped
``{"symbol": "<device symbol>", "parameters": ["<name>", ...]}``. Two properties of
it carry the whole reason this module exists:

* **The array position is the model's identity on the wire.** A preset stores a
  block's model as an index into this array, not as a name.
* **Device symbols carry a ``Mono``/``Stereo`` suffix that the host-side catalog
  does not**, and the two variants have different parameter counts *and* different
  orders. A model's value vector is in the order of the variant actually in use, so
  aligning it against the host catalog's order mislabels every parameter past the
  first one that variant omits.

This file is Line 6's and is deliberately not vendored into this repository. Point
:meth:`DeviceSymbols.load` at a copy taken from your own HX Edit installation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Channel-count suffixes the device appends to a host symbol. The empty string
#: covers models with no variants at all, which is most amps and cabs.
VARIANTS: tuple[str, ...] = ("Mono", "Stereo")


class SymbolsError(RuntimeError):
    """Raised when ``Helix.sym`` cannot be read or does not have the expected shape."""


def normalize_parameter_name(name: str) -> str:
    """Fold a parameter name to a form comparable across spellings.

    The same control is spelled differently by different eras of HX Edit and by the
    two sides of this bridge — ``High Cut`` against ``HighCut``, ``Gate_Range``
    against ``GateRange``. Matching on a form with separators removed and case
    folded is what makes the join land.
    """
    return "".join(ch for ch in name if ch.isalnum()).casefold()


@dataclass(frozen=True)
class DeviceSymbol:
    """One entry of ``Helix.sym``."""

    #: Full device symbol, e.g. ``HD2_TremoloHarmonicMono``.
    symbol: str
    #: Position in the array. This is the number a preset stores for the model.
    index: int
    #: Parameter names in the order the device's value vector uses.
    parameters: tuple[str, ...]

    def ordinal_of(self, parameter_name: str) -> int | None:
        """Index of ``parameter_name`` in this symbol's order, or ``None``."""
        wanted = normalize_parameter_name(parameter_name)
        for position, candidate in enumerate(self.parameters):
            if normalize_parameter_name(candidate) == wanted:
                return position
        return None


class DeviceSymbols:
    """The parsed ``Helix.sym`` table."""

    def __init__(self, entries: list[DeviceSymbol]):
        self._ordered = entries
        self._by_symbol = {entry.symbol: entry for entry in entries}

    # ---- construction -----------------------------------------------------

    @classmethod
    def parse(cls, payload: bytes | str) -> DeviceSymbols:
        """Parse ``Helix.sym`` content."""
        try:
            data: Any = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SymbolsError(f"Helix.sym is not valid JSON: {exc}") from exc
        if not isinstance(data, list):
            raise SymbolsError(
                f"Helix.sym must be a JSON array, got {type(data).__name__}"
            )

        entries: list[DeviceSymbol] = []
        for index, raw in enumerate(data):
            if not isinstance(raw, dict):
                raise SymbolsError(f"Helix.sym entry {index} is not an object")
            symbol = raw.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise SymbolsError(f"Helix.sym entry {index} has no 'symbol' string")
            raw_parameters = raw.get("parameters", [])
            if raw_parameters is None:
                raw_parameters = []
            if not isinstance(raw_parameters, list):
                raise SymbolsError(
                    f"Helix.sym entry {index} ('{symbol}') has a non-list 'parameters'"
                )
            parameters = tuple(str(name) for name in raw_parameters)
            entries.append(DeviceSymbol(symbol=symbol, index=index, parameters=parameters))
        return cls(entries)

    @classmethod
    def load(cls, path: Path) -> DeviceSymbols:
        """Read ``Helix.sym`` from disk."""
        path = Path(path)
        if not path.exists():
            raise SymbolsError(
                f"Helix.sym not found at {path}. Copy it out of your own HX Edit "
                "installation; it is not distributed with this project."
            )
        return cls.parse(path.read_bytes())

    # ---- lookups ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._ordered)

    def __iter__(self):
        return iter(self._ordered)

    def by_index(self, index: int) -> DeviceSymbol | None:
        """The symbol a preset's stored model number refers to."""
        if 0 <= index < len(self._ordered):
            return self._ordered[index]
        return None

    def by_symbol(self, symbol: str) -> DeviceSymbol | None:
        """Look a full device symbol up by name."""
        return self._by_symbol.get(symbol)

    def index_of(self, symbol: str) -> int | None:
        """The number to store for ``symbol``, the inverse of :meth:`by_index`."""
        entry = self._by_symbol.get(symbol)
        return None if entry is None else entry.index

    # ---- variants ---------------------------------------------------------

    def variants_of(self, host_symbol: str) -> list[str]:
        """Which channel-count suffixes the device actually offers for a host model.

        Returns the suffixes to append: ``[""]`` where the device has a single
        unsuffixed symbol, one entry where only one variant exists (reverbs are
        commonly stereo-only), and both where there is a real choice. An empty list
        means the host model has no device symbol at all.

        This is what makes a missing ``@stereo`` readable in a ``.hlx``: HX Edit
        writes the flag only where something can be chosen, so its absence means
        "the variant that exists", *not* "Mono".
        """
        found = [suffix for suffix in VARIANTS if f"{host_symbol}{suffix}" in self._by_symbol]
        if not found and host_symbol in self._by_symbol:
            return [""]
        return found

    def resolve(self, host_symbol: str, *, stereo: bool | None = None) -> DeviceSymbol | None:
        """Pick the device symbol for a host model and an optional ``@stereo`` flag.

        With ``stereo`` unset, or set to a variant the device does not offer, the
        single available variant is returned; where both exist and no preference is
        given, ``Mono`` is chosen as HX Edit's own default.
        """
        variants = self.variants_of(host_symbol)
        if not variants:
            return None
        if variants == [""]:
            return self._by_symbol.get(host_symbol)

        preferred = "Stereo" if stereo else "Mono"
        if stereo is not None and preferred in variants:
            chosen = preferred
        elif len(variants) == 1:
            chosen = variants[0]
        else:
            chosen = "Mono" if "Mono" in variants else variants[0]
        return self._by_symbol.get(f"{host_symbol}{chosen}")

    def resolve_by_value_count(
        self, host_symbol: str, observed: int, *, trailing_extras: int = 1
    ) -> DeviceSymbol | None:
        """Pick the variant whose parameter count matches an observed value vector.

        Some blocks store extra values past the symbol's own parameter list — a
        reverb's ``Trails`` switch, an amp+cab's ``@mic``. ``trailing_extras`` is how
        many such values are tolerated, so a symbol of length *n* matches a vector of
        length *n* through *n + trailing_extras*.
        """
        candidates = [
            entry
            for suffix in self.variants_of(host_symbol)
            if (entry := self._by_symbol.get(f"{host_symbol}{suffix}")) is not None
        ]
        exact = [e for e in candidates if len(e.parameters) == observed]
        if exact:
            return exact[0]
        near = [
            e
            for e in candidates
            if len(e.parameters) < observed <= len(e.parameters) + trailing_extras
        ]
        return near[0] if near else None
