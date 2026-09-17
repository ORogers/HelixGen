"""Converting an ``.hlx`` tone into a device document, over a donor.

We do **not** synthesise a document. A device preset is not fully understood --
preset key ``5`` alone holds 86 mostly-undecoded fields, and the input/output
nodes store a "ragged prefix" of their model's parameter list that nobody has
pinned the rule for. So the write path takes a document the device itself wrote,
read back from the target slot, and overlays only the fields the ``.hlx``
determines. Everything else keeps the device's own bytes.

This is the same pattern ``generate_preset()`` already uses one level up, where
it deep-copies ``HXTemplate.hlx`` rather than building a preset from nothing.

The mapping this implements:

===========================  ==============================================
``.hlx``                     wire
===========================  ==============================================
``@model`` + ``@stereo``     the device symbol's index, at block ``24 -> 25``
named parameters             ``11 -> 4``, in that symbol's parameter order
``@path``, ``@position``     the donor's slot run for that path, at that
                             position
``@enabled``                 content key ``10``
===========================  ==============================================

Two rules in there are load-bearing and easy to get backwards:

* **``@stereo`` is written only when the model has both variants.** An absent
  ``@stereo`` means *the variant that exists*, not "Mono". Reading it as Mono
  makes every stereo-only model -- which is every reverb -- unresolvable.
* **A parameter the tone does not supply is left at the donor's value**, not
  zeroed. Unsupplied entries must never shift the ones after them, because 43 of
  the device's 153 Mono/Stereo pairs diverge mid-list.

Anything a tone asks for that the wire cannot carry is collected in
:class:`OverlayReport` rather than guessed at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from hlxgen.device.document import Document
from hlxgen.device.symbols import DeviceSymbols, normalize_parameter_name

logger = logging.getLogger(__name__)

#: Keys in an ``.hlx`` block that are structure, not parameters.
STRUCTURAL_KEYS = frozenset(
    {
        "@model",
        "@path",
        "@position",
        "@type",
        "@enabled",
        "@stereo",
        "@no_snapshot_bypass",
        "@cab",
        "@mic",
        "@trails",
        "@fs_index",
        "@fs_label",
        "@fs_ledcolor",
        "@fs_enabled",
    }
)


class CodecError(RuntimeError):
    """Raised when a tone cannot be laid over a donor."""


@dataclass
class BlockReport:
    """What happened to one block."""

    name: str
    model: str
    record_index: int
    device_index: int
    symbol: str
    applied: dict[str, Any] = field(default_factory=dict)
    #: Parameters the tone supplied that this symbol has no slot for.
    unmapped: list[str] = field(default_factory=list)
    #: Parameters the symbol has that the tone did not supply; the donor's
    #: values are kept for these.
    from_donor: list[str] = field(default_factory=list)


@dataclass
class OverlayReport:
    """What the overlay could and could not carry."""

    blocks: list[BlockReport] = field(default_factory=list)
    #: Blocks skipped because the donor has no record at their slot.
    skipped: list[str] = field(default_factory=list)
    #: Blocks that landed on an empty donor slot and had a record shaped for
    #: them from a sibling block.
    materialized: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.skipped and not any(b.unmapped for b in self.blocks)

    def summary(self) -> str:
        lines = [f"{len(self.blocks)} block(s) overlaid"]
        for block in self.blocks:
            detail = f"  {block.name}: {block.model} -> index {block.device_index}"
            if block.unmapped:
                detail += f"  [unmapped: {', '.join(block.unmapped)}]"
            if block.from_donor:
                detail += f"  [donor keeps: {', '.join(block.from_donor)}]"
            lines.append(detail)
        lines.extend(f"  {name}: filled an empty donor slot" for name in self.materialized)
        lines.extend(
            f"  {name}: SKIPPED -- donor has no record at that slot"
            for name in self.skipped
        )
        return "\n".join(lines)


def record_index_for(donor: Document, path: int, position: int) -> int | None:
    """Record index of the donor slot a block at ``@path``/``@position`` lands in.

    Resolved against the donor's own slot runs rather than by arithmetic on the
    record list: the document's leading stream bytes decode as records, so a
    slot's list index is offset from its wire slot number by an amount that is
    not fixed across documents.
    """
    return donor.record_for(path, position)


def stored_value_count(symbol: Any) -> int:
    """How many values the device actually stores for a model.

    It is **not** simply the symbol's parameter count. Measured across 126
    presets read off a real HX Stomp:

    * A symbol carrying ``IrData`` stores **one fewer** -- the IR payload is
      never part of the value vector, and it is always the trailing entry, so
      dropping it shifts nothing (checked: last in all 92 symbols that have it).
    * Most other symbols store exactly their parameter count.
    * Some -- reverbs, delays, FX loops -- store **one more**, the trailing
      extra a ``Trails`` switch lives in. That one is appended by the device
      rather than by us.

    Writing the wrong count is not a cosmetic error: the device rejects the whole
    document and stores an empty preset in its place.
    """
    count = len(symbol.parameters)
    if "IrData" in symbol.parameters:
        count -= 1
    return count


def tone_blocks(preset: dict) -> list[tuple[str, dict]]:
    """Every real effect block in a preset, in document order.

    The DSP objects also carry ``split``/``join``/``inputA``/``outputB`` entries,
    which are routing nodes rather than blocks and have no ``@position`` pair to
    place them by.
    """
    tone = preset.get("data", {}).get("tone", {})
    found: list[tuple[str, dict]] = []
    for dsp_name in sorted(k for k in tone if k.startswith("dsp")):
        dsp = tone[dsp_name]
        if not isinstance(dsp, dict):
            continue
        for block_name in sorted(k for k in dsp if k.startswith("block")):
            block = dsp[block_name]
            if isinstance(block, dict) and "@model" in block:
                found.append((f"{dsp_name}.{block_name}", block))
    return found


def _resolve_symbol(symbols: DeviceSymbols, block: dict) -> Any:
    """Resolve a block's ``@model`` to a device symbol.

    ``@stereo`` is consulted only when the model actually has both variants --
    an absent or False flag on a stereo-only model still has to land on the
    stereo symbol.
    """
    model = block["@model"]
    variants = symbols.variants_of(model)
    if not variants:
        raise CodecError(f"{model!r} has no device symbol in Helix.sym")

    # Pass the flag through only when the tone actually carries one. Forcing a
    # default here would re-introduce exactly the bug the absence rule exists to
    # prevent: resolve() picks the sole available variant when there is no
    # choice, and HX Edit's own Mono default only when there is.
    stereo = bool(block["@stereo"]) if "@stereo" in block else None
    resolved = symbols.resolve(model, stereo=stereo)
    if resolved is None:
        raise CodecError(f"{model!r} offers variants {variants} but none resolved")
    return resolved


def overlay(
    donor: Document,
    preset: dict,
    symbols: DeviceSymbols,
) -> OverlayReport:
    """Lay a tone over a donor document, in place.

    Only blocks the tone names are touched. A donor slot the tone says nothing
    about keeps the device's bytes, which is the point of using a donor at all.
    """
    report = OverlayReport()
    materialized: list[str] = []

    for name, block in tone_blocks(preset):
        path = int(block.get("@path", 0))
        position = int(block.get("@position", 0))
        index = record_index_for(donor, path, position)

        if index is None:
            report.skipped.append(name)
            logger.warning(
                "%s sits at path %d position %d, which the donor's slot array "
                "does not reach",
                name,
                path,
                position,
            )
            continue

        symbol = _resolve_symbol(symbols, block)

        # Captured before anything is mutated. Whether the donor's value vector
        # still means anything depends on the model it was written for.
        original_model = donor.model_index(index, path)

        if donor.is_empty_slot(index, path):
            template = donor.first_block(path)
            if template is None:
                report.skipped.append(name)
                logger.warning(
                    "%s lands on an empty slot and the donor has no block to "
                    "shape one from",
                    name,
                )
                continue
            donor.materialize_slot(index, template, path)
            materialized.append(name)

        donor.set_model_index(index, symbol.index, path)

        if "@enabled" in block:
            donor.set_enabled(index, bool(block["@enabled"]), path)

        values, applied, unmapped, from_donor = _value_vector(
            donor, index, block, symbol, path=path, original_model=original_model
        )
        donor.set_values(index, values, path)

        report.blocks.append(
            BlockReport(
                name=name,
                model=block["@model"],
                record_index=index,
                device_index=symbol.index,
                symbol=symbol.symbol,
                applied=applied,
                unmapped=unmapped,
                from_donor=from_donor,
            )
        )

    report.materialized = materialized
    return report


def _value_vector(
    donor: Document,
    index: int,
    block: dict,
    symbol: Any,
    *,
    path: int,
    original_model: int | None,
) -> tuple[list[Any], dict[str, Any], list[str], list[str]]:
    """Lay the tone's named parameters out in the symbol's device order.

    **The donor's vector is only a usable base when the model has not changed.**
    A value vector is positional in one specific symbol's parameter order, so the
    moment a slot's model changes, the old values mean nothing at their old
    positions -- and since 43 of the device's 153 Mono/Stereo pairs diverge
    mid-list, carrying them over would land real numbers on the wrong controls.
    When the model changes the vector is rebuilt at the new symbol's length, and
    whatever the tone does not supply is reported rather than inherited.

    When the model is unchanged the donor's own length is kept, which preserves
    the trailing symbol entries that carry no stored value -- every cab's
    ``IrData`` -- instead of inventing one.
    """
    donor_values = donor.values(index, path) or []
    same_model = original_model is not None and original_model == symbol.index

    if same_model and donor_values:
        vector: list[Any] = list(donor_values)
    else:
        vector = [0.0] * stored_value_count(symbol)

    by_name = {
        normalize_parameter_name(param): position
        for position, param in enumerate(symbol.parameters)
    }

    applied: dict[str, Any] = {}
    unmapped: list[str] = []
    supplied: set[int] = set()

    for key, value in block.items():
        if key in STRUCTURAL_KEYS or key.startswith("@"):
            continue
        position = by_name.get(normalize_parameter_name(key))
        if position is None or position >= len(vector):
            unmapped.append(key)
            continue
        vector[position] = value
        applied[key] = value
        supplied.add(position)

    unfilled = [
        param
        for position, param in enumerate(symbol.parameters)
        if position < len(vector) and position not in supplied
    ]

    return vector, applied, unmapped, unfilled
