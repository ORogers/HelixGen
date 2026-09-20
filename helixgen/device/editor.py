"""Surgical block edits -- path A.

A whole-document write (``op 21``) can change a block's **values** but not its
**model**: the device accepts the transfer, commits it, and stores an empty
preset. Whatever it validates about a model is not carried in the block record,
and is undecoded. Changing a model is what these ops are for.

The sequence for putting a generated tone on the pedal:

1. ``op 20`` select the target slot, so the edit buffer holds it.
2. **Read**, which is what opens the buffer. Never edit straight after a select.
3. Per block: ``op 40`` swap the model, then ``op 30`` for each parameter the
   tone names, then ``op 41`` for its bypass state.
4. ``op 71`` commit the buffer to the slot.

``op 40`` **resets the block's parameters to the new model's defaults**, which is
why the value edits follow the swap and not the other way round -- and why a tone
that names only some parameters still lands on sensible values for the rest.

Every edit is acknowledged, and **the acknowledgement is matched by the
transaction id echoed at key 102**, never by arrival order. The device
interleaves keepalives, credits and status pushes on the same pipe; taking one of
those as the ACK reports an edit as applied on the strength of a frame that says
nothing about it, and shifts every later reply by one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

OP_SELECT_PRESET: Final = 20
OP_DELETE_BLOCK: Final = 28
OP_SET_VALUE: Final = 30
OP_SWAP_MODEL: Final = 40
OP_BYPASS: Final = 41
OP_SAVE_PRESET: Final = 71

#: Target keys.
K_SLOT: Final = 98
K_BANK: Final = 107
K_PRESET: Final = 108
K_NAME: Final = 109
#: The model-ref sub-map, nested *inside* the target. Distinct from the
#: envelope's own key 100, which is the operation.
K_MODEL_REF: Final = 100
K_MODEL_FLAG: Final = 23
K_MODEL_INDEX: Final = 25
K_PAIRED_INDEX: Final = 26
#: On a set-value: which index space key 28 addresses, and the value itself.
K_IS_PARAM: Final = 29
K_MODEL_SEL: Final = 26
K_PARAM_INDEX: Final = 28
K_VALUE: Final = 119
#: Carries **enabled**, despite the op being called bypass. See set_enabled.
K_ENABLED: Final = 59

#: Sub-model selectors for a set-value.
MODEL_MAIN: Final = 0
MODEL_PAIRED: Final = 1

#: ``-306`` on a swap means the model does not fit the DSP budget.
ERR_DSP_BUDGET: Final = -306

NO_PAIRED: Final = -1


class EditError(RuntimeError):
    """Raised when the device refuses an edit."""


class DspBudgetError(EditError):
    """Raised when a model will not fit in the remaining DSP budget."""


@dataclass
class EditReport:
    """What an apply actually managed to do."""

    swapped: list[str] = field(default_factory=list)
    values_set: int = 0
    #: Blocks the tone asked to be switched off.
    bypassed: list[str] = field(default_factory=list)
    footswitches: list[str] = field(default_factory=list)
    snapshots: list[str] = field(default_factory=list)
    #: Parameters put under snapshot control, as ``"dsp0.block2.Drive"``.
    snapshot_controls: list[str] = field(default_factory=list)
    #: Controller assignments the tone makes that could not be written.
    unsupported_controllers: list[str] = field(default_factory=list)
    #: How many USB sessions the run needed; it renews before the sequence wrap.
    sessions: int = 1
    #: Parameters skipped because their value had no wire representation.
    unsupported: list[str] = field(default_factory=list)
    #: Blocks that could not be placed at all.
    skipped: list[str] = field(default_factory=list)
    #: Cab blocks absorbed into an amp's own slot, as ``"block -> cab symbol"``.
    fused: list[str] = field(default_factory=list)
    #: Cab parameters that the paired cab model has no home for.
    dropped_cab_params: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"{len(self.swapped)} model(s) swapped, {self.values_set} value(s) set, "
            f"{len(self.bypassed)} block(s) left off"
        ]
        lines.extend(f"  fused {entry}" for entry in self.fused)
        if self.dropped_cab_params:
            lines.append(
                f"  cab params with no paired equivalent: "
                f"{', '.join(self.dropped_cab_params)}"
            )
        if self.footswitches:
            lines.append(f"  footswitches: {', '.join(self.footswitches)}")
        if self.snapshots:
            lines.append(f"  snapshots: {', '.join(self.snapshots)}")
        if self.snapshot_controls:
            lines.append(f"  snapshot-controlled: {', '.join(self.snapshot_controls)}")
        if self.unsupported_controllers:
            lines.append(
                f"  controllers not written: {', '.join(self.unsupported_controllers)}"
            )
        lines.extend(f"  swapped {name}" for name in self.swapped)
        if self.unsupported:
            lines.append(f"  unsupported values: {', '.join(self.unsupported)}")
        lines.extend(f"  SKIPPED {name}" for name in self.skipped)
        return "\n".join(lines)


def enum_index(catalog: Any, model_name: str, parameter: str, label: str) -> int | None:
    """Resolve a parameter value written as a label to its wire index.

    An ``.hlx`` names enum values the way the UI shows them -- a compressor's
    ratio is ``"2:1"``, not ``0``. The catalog carries the mapping, so this is a
    lookup rather than a guess; without a catalog the value simply has no wire
    form and is reported.
    """
    if catalog is None:
        return None
    try:
        model = catalog.get(model_name)
        definition = model.get_parameter(parameter)
    except Exception:  # noqa: BLE001 - an unknown model or parameter is not fatal
        return None
    index = getattr(definition, "reverse_map", {}).get(label)
    return index if isinstance(index, int) else None


def _wire_candidates(value: Any) -> list[Any]:
    """Wire forms to try for an ``.hlx`` value, most likely first.

    A bool is only ever a switch. A number may be either a continuous parameter
    (float32) or an enum index (int), and the JSON type does not settle it --
    ``Angle: 45`` is a continuous mic angle while ``Mic: 3`` is an enum. Strings
    name an enum by its label and have no wire form without the catalog's value
    list, so they get none.
    """
    if isinstance(value, bool):
        return [value]
    if isinstance(value, int):
        # 0/1 may be a switch written as a number -- an amp's hum switch is one.
        if value in (0, 1):
            return [float(value), value, bool(value)]
        return [float(value), value]
    if isinstance(value, float):
        return [float(value), int(value)] if value.is_integer() else [float(value)]
    return []


def wire_value(value: Any) -> Any | None:
    """Convert an ``.hlx`` parameter value to its wire form.

    **The type must match the parameter exactly** -- the device refuses a value
    of the wrong type rather than coercing it. Continuous knobs are floats,
    enum/list parameters are ints, switches are bools. A string (an enum named by
    its label, like a compressor's ``"2:1"``) has no representation here without
    the catalog's value list, so it comes back ``None`` to be reported rather
    than guessed at.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return float(value)
    return None


class BlockEditor:
    """Drives surgical edits over an open :class:`~helixgen.device.usb.Session`."""

    def __init__(self, session: Any):
        self.session = session

    # -- primitives --------------------------------------------------------

    def select_preset(self, bank: int, slot: int) -> None:
        """Load a preset into the edit buffer. **Changes what the pedal shows.**"""
        self._edit(OP_SELECT_PRESET, {K_BANK: bank, K_PRESET: slot})

    def swap_model(self, slot: int, model_index: int, paired: int = NO_PAIRED) -> None:
        """Replace the model of the block in ``slot``.

        The device resets that block's parameters to the new model's defaults, so
        any value the tone specifies must be sent afterwards.

        ``23`` is the paired-model-active flag and mirrors ``paired >= 0``: with
        it false the device stores the paired index but never instantiates the
        cab.
        """
        try:
            self._edit(
                OP_SWAP_MODEL,
                {
                    K_SLOT: slot,
                    K_MODEL_REF: {
                        K_MODEL_FLAG: paired >= 0,
                        K_MODEL_INDEX: model_index,
                        K_PAIRED_INDEX: paired,
                    },
                },
            )
        except EditError as exc:
            if str(ERR_DSP_BUDGET) in str(exc):
                raise DspBudgetError(
                    f"Model {model_index} does not fit the remaining DSP budget "
                    f"at slot {slot}."
                ) from exc
            raise

    def set_value(
        self,
        slot: int,
        param_index: int,
        value: Any,
        *,
        model_sel: int = MODEL_MAIN,
    ) -> None:
        """Set one parameter of a block."""
        self._edit(
            OP_SET_VALUE,
            {
                K_SLOT: slot,
                K_IS_PARAM: True,
                K_MODEL_SEL: model_sel,
                K_PARAM_INDEX: param_index,
                K_VALUE: value,
            },
        )

    def try_set_value(
        self, slot: int, param_index: int, raw: Any, *, model_sel: int = MODEL_MAIN
    ) -> bool:
        """Set a parameter, resolving its wire type by trying the plausible ones.

        The device enforces a parameter's type exactly and refuses anything else,
        but an ``.hlx`` does not say which type a parameter is: a mic distance
        reads as ``1`` and an amp drive as ``0.2``, and both are continuous. Since
        a refusal is cheap and harmless -- the device rejects the edit and
        changes nothing -- the type is resolved by attempting the candidates
        rather than by guessing from the JSON type alone.

        Returns whether any candidate was accepted.
        """
        for candidate in _wire_candidates(raw):
            try:
                self.set_value(slot, param_index, candidate, model_sel=model_sel)
            except EditError:
                continue
            return True
        return False

    def set_enabled(self, slot: int, enabled: bool) -> None:
        """Switch a block on or off.

        An explicit state, not a toggle -- the device takes the bool.

        **Key 59 carries *enabled*, not *bypassed*.** The op is named "bypass"
        in the prior art, and its capture is a bypass press sending ``true`` then
        ``false``, which says nothing about polarity. Measured directly against a
        block's stored ``enabled`` flag: ``59: true`` turns the block **on**.
        Reading the name as "bypassed" inverts every block in the preset, which
        is silent -- the write succeeds and the chain is correct, it is just
        entirely switched off.
        """
        self._edit(OP_BYPASS, {K_SLOT: slot, K_ENABLED: enabled})

    def save_preset(self, bank: int, slot: int, name: str) -> None:
        """Commit the edit buffer to flash. Nothing is persistent before this."""
        self._edit(OP_SAVE_PRESET, {K_BANK: bank, K_PRESET: slot, K_NAME: name + "\x00"})

    # -- transport ---------------------------------------------------------

    def _edit(self, op: int, target: Any) -> Any:
        from helixgen.device.usb import DeviceRefusedError

        try:
            return self.session.command(op=op, target=target)
        except DeviceRefusedError as exc:
            raise EditError(f"the device refused op {op}: {exc}") from exc

    def delete_block(self, slot: int) -> None:
        """Empty a slot.

        Surgical: the device clears the slot and drops any footswitch binding of
        that block alone, leaving other bindings intact.
        """
        self._edit(OP_DELETE_BLOCK, {K_SLOT: slot})


class EditSession:
    """A session that renews itself before the sequence wrap kills it.

    Applying a tone costs far more frames than one session survives: two full
    document reads, a swap and a handful of value edits per block, and a
    whole-document write at the end. The header's sequence byte wraps at 256 and
    the device stops answering across the rollover, so a long edit run has to be
    split across sessions.

    Renewing is not just reconnecting. The edit buffer is the device's, tied to
    the loaded preset, so the run **commits before it lets go** and re-selects
    and re-reads afterwards -- the read being what reopens the buffer. Dropping
    the session without committing would discard every edit since the last save.
    """

    def __init__(self, bank: int, slot: int, name: str, info: Any = None):
        from helixgen.device.usb import Session

        self._open = Session.open
        self.bank = bank
        self.slot = slot
        self.name = name
        self.info = info
        self.session = self._open(info)
        self.sessions = 1
        self._prepare()

    def _prepare(self) -> None:
        """Load the slot and open its edit buffer."""
        BlockEditor(self.session).select_preset(self.bank, self.slot)
        # The read is what opens the edit buffer; editing straight after a
        # select stalls deterministically.
        self.document = self.session.read_slot(bank=self.bank, slot=self.slot)

    def renew_if_needed(self) -> None:
        if not self.session.near_sequence_wrap:
            return
        BlockEditor(self.session).save_preset(self.bank, self.slot, self.name)
        self.session.close()
        self.session = self._open(self.info)
        self.sessions += 1
        self._prepare()

    def commit(self) -> None:
        BlockEditor(self.session).save_preset(self.bank, self.slot, self.name)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> EditSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def plan_chain(
    preset: dict,
    symbols: Any,
    amps: Any,
    *,
    fuse_cabs: bool = True,
) -> tuple[list[dict], list[str]]:
    """Work out what goes in each slot, fusing each amp with its own cab.

    An amp and its cab are **one block** on the pedal, not two: the cab rides in
    the amp's slot as a paired model, which is how a preset with an amp and six
    effects still fits a Stomp's eight slots. A tone that lists them separately
    is describing the same thing in two slots, so the cab is absorbed and the
    chain closes up behind it.

    The cab used is the amp's own default (``cablink``), not whatever cab the
    tone named. That is not a shortcut: a paired cab is a **different model**
    from a standalone one -- no mic, position or angle -- and only 20 of the 92
    standalone cabs have a paired counterpart at all, so honouring the tone's
    choice would fail for most of them.

    Returns the planned entries and the names of the absorbed cab blocks.
    """
    from helixgen.device.codec import _resolve_symbol, tone_blocks

    blocks = tone_blocks(preset)
    resolved = [(n, b, _resolve_symbol(symbols, b)) for n, b in blocks]

    planned: list[dict] = []
    absorbed: list[str] = []
    index = 0
    while index < len(resolved):
        block_name, block, symbol = resolved[index]
        paired = NO_PAIRED
        cab_block = None
        cab_name = None

        if fuse_cabs and amps is not None and amps.is_amp(symbol.symbol):
            cab_symbol = amps.paired_cab(symbol.symbol)
            entry = symbols.by_symbol(cab_symbol) if cab_symbol else None
            if entry is not None:
                paired = entry.index
                # A cab the tone lists right after the amp is that same cab
                # described separately; take its settings and drop the block.
                if index + 1 < len(resolved):
                    following = resolved[index + 1]
                    if following[2].symbol.startswith("HD2_Cab"):
                        cab_block = following[1]
                        cab_name = following[0]
                        asked = following[2].symbol
                        note = f"{following[0]} -> {entry.symbol}"
                        if asked != entry.symbol:
                            # The amp's own cab replaces the tone's choice; say
                            # so, because it is an audible substitution.
                            note += f" (tone asked for {asked})"
                        absorbed.append(note)
                        index += 1

        planned.append(
            {
                "name": block_name,
                "block": block,
                "symbol": symbol,
                "paired": paired,
                "paired_symbol": symbols.by_index(paired) if paired >= 0 else None,
                "cab_block": cab_block,
                "cab_name": cab_name,
            }
        )
        index += 1

    return planned, absorbed


def apply_tone(
    preset: dict,
    symbols: Any,
    *,
    bank: int = 0,
    slot: int,
    name: str | None = None,
    catalog: Any = None,
    info: Any = None,
    amps: Any = None,
    fuse_cabs: bool = True,
) -> EditReport:
    """Put a generated tone on the pedal with surgical edits, then commit.

    This is the path that can change models, which a whole-document write cannot.
    The order matters: the select loads the slot, the read opens its edit buffer,
    each swap resets that block's parameters to the new model's defaults, and only
    then are the tone's own values sent.
    """
    from helixgen.device.document import parse

    report = EditReport()
    preset_name = name or preset.get("data", {}).get("meta", {}).get("name") or "helixgen"

    run = EditSession(bank, slot, preset_name, info)
    if run.document is None:
        run.close()
        raise EditError(f"slot {slot} is unpopulated; select a slot holding a preset")

    parsed = parse(run.document)
    planned, absorbed = plan_chain(preset, symbols, amps, fuse_cabs=fuse_cabs)
    report.fused.extend(absorbed)

    # Positions are assigned in chain order rather than taken from the tone's own
    # @position: fusing a cab into its amp closes a gap, and leaving one would
    # waste the slot the fusion just freed.
    wanted: dict[int, dict] = {}
    for position, entry in enumerate(planned):
        index = parsed.record_for(0, position)
        if index is None:
            report.skipped.append(entry["name"])
            continue
        wanted[index] = entry

    # Slots the donor fills that the tone does not: empty them, so the pedal ends
    # up with the generated chain rather than a merge of two.
    for index in parsed.block_slots():
        if index not in wanted:
            run.renew_if_needed()
            try:
                BlockEditor(run.session).delete_block(index)
            except EditError as exc:
                logger.debug("Could not clear slot %d: %s", index, exc)

    for index in sorted(wanted):
        entry = wanted[index]
        block_name, block, symbol = entry["name"], entry["block"], entry["symbol"]
        run.renew_if_needed()
        editor = BlockEditor(run.session)
        try:
            editor.swap_model(index, symbol.index, paired=entry["paired"])
        except EditError as exc:
            logger.warning("%s: %s", block_name, exc)
            report.skipped.append(block_name)
            continue
        paired_symbol = entry["paired_symbol"]
        detail = f"{block_name} -> {symbol.symbol}"
        if paired_symbol is not None:
            detail += f" + {paired_symbol.symbol}"
        report.swapped.append(detail)

        for key, raw in block.items():
            if key.startswith("@"):
                continue
            ordinal = symbol.ordinal_of(key)
            if ordinal is None:
                continue
            if isinstance(raw, str):
                resolved = enum_index(catalog, block["@model"], key, raw)
                if resolved is None:
                    report.unsupported.append(f"{block_name}.{key}")
                    continue
                raw = resolved
            run.renew_if_needed()
            if not BlockEditor(run.session).try_set_value(index, ordinal, raw):
                report.unsupported.append(f"{block_name}.{key}")
                continue
            report.values_set += 1

        # The paired cab's own parameters, where the absorbed cab block named
        # any the paired model actually has.
        if entry["cab_block"] is not None and paired_symbol is not None:
            run.renew_if_needed()
            _apply_cab_values(
                BlockEditor(run.session), index, entry["cab_block"], paired_symbol, report
            )

        # Always sent, not only when the tone says so: a swap leaves the block
        # in whatever state the slot's previous occupant had, and a generated
        # preset's blocks should be live unless it asks otherwise.
        enabled = bool(block.get("@enabled", True))
        run.renew_if_needed()
        try:
            BlockEditor(run.session).set_enabled(index, enabled)
            if not enabled:
                report.bypassed.append(block_name)
        except EditError as exc:
            logger.debug("%s enable refused: %s", block_name, exc)

    run.commit()

    # The chain is now correct on the device. The footswitch layout and the
    # snapshots live in the document rather than behind edit ops, so they go back
    # as a whole-document write -- which the device accepts, because by this
    # point no model is changing.
    # Absorbed cabs are deliberately absent from `placed`, so a footswitch the
    # tone bound to one is simply not written. A paired cab is part of the amp's
    # block and cannot be switched on its own, and remapping it onto the amp
    # would just give two switches that toggle the same thing.
    placed = {entry["name"]: index for index, entry in wanted.items()}
    # Where a controller aimed at a tone block lands: the block's own slot and
    # model, or -- for a cab fused into its amp -- the amp's slot and paired model.
    targets: dict[str, ControllerTarget] = {}
    for index, entry in wanted.items():
        targets[entry["name"]] = ControllerTarget(index, entry["symbol"], MODEL_MAIN, entry["block"])
        if entry["cab_name"] is not None and entry["paired_symbol"] is not None:
            targets[entry["cab_name"]] = ControllerTarget(
                index, entry["paired_symbol"], MODEL_PAIRED, entry["cab_block"]
            )
    try:
        # A fresh session for the layout: the run above has spent most of its
        # frame budget, and the write is another dozen on top of a full read.
        run.session.close()
        run.session = run._open(info)
        run.sessions += 1
        _apply_layout(
            run.session,
            preset,
            placed,
            targets,
            bank=bank,
            slot=slot,
            name=preset_name,
            catalog=catalog,
            report=report,
        )
    finally:
        run.close()

    report.sessions = run.sessions
    return report


def _apply_cab_values(
    editor: BlockEditor, slot: int, cab_block: dict, cab_symbol: Any, report: EditReport
) -> None:
    """Carry an absorbed cab block's settings onto the paired cab.

    Selecting the paired sub-model (``26: 1``) is what distinguishes these from
    the amp's own parameters; without it a mic-distance edit lands on the amp's
    Mid, because both are parameter 2 of their respective models.

    A paired cab has no mic, position or angle -- those belong to a standalone
    cab -- so parameters with no equivalent are reported rather than forced.
    """
    for key, raw in cab_block.items():
        if key.startswith("@"):
            continue
        ordinal = cab_symbol.ordinal_of(key)
        if ordinal is None:
            report.dropped_cab_params.append(key)
            continue
        if editor.try_set_value(slot, ordinal, raw, model_sel=MODEL_PAIRED):
            report.values_set += 1
        else:
            report.unsupported.append(f"cab.{key}")


@dataclass(frozen=True)
class ControllerTarget:
    """The device address of a tone block's parameters."""

    slot: int
    symbol: Any
    model_sel: int
    block: dict


def _apply_layout(
    session: Any,
    preset: dict,
    placed: dict[str, int],
    targets: dict[str, ControllerTarget],
    *,
    bank: int,
    slot: int,
    name: str,
    catalog: Any,
    report: EditReport,
) -> None:
    """Write the footswitch layout and snapshots, which the edit ops do not carry.

    ``placed`` maps each tone block's name to the device slot it ended up in, so a
    switch bound to ``dsp0.block2`` reaches the block that actually holds it --
    the two are independent on the wire, and real presets routinely bind switch 1
    to a block deep in the chain.
    """
    from helixgen.device.document import dump, parse

    raw = session.read_slot(bank=bank, slot=slot)
    if raw is None:
        return
    document = parse(raw)
    tone = preset.get("data", {}).get("tone", {})

    assigned: set[int] = set()
    for dsp_name, blocks in (tone.get("footswitch") or {}).items():
        if not isinstance(blocks, dict):
            continue
        for block_id, assignment in blocks.items():
            if not isinstance(assignment, dict):
                continue
            switch = assignment.get("@fs_index")
            target = placed.get(f"{dsp_name}.{block_id}")
            if switch is None or target is None:
                continue
            ok = document.set_footswitch(
                int(switch) - 1,
                block_slot=target,
                label=str(assignment.get("@fs_label") or ""),
                color=assignment.get("@fs_ledcolor"),
                enabled=bool(assignment.get("@fs_enabled", True)),
            )
            if ok:
                assigned.add(int(switch) - 1)
                report.footswitches.append(f"FS{switch}->{dsp_name}.{block_id}")

    # A switch the tone does not mention must not keep the previous preset's
    # binding, which would point at a block that is no longer there.
    if assigned:
        for position in range(len(document.footswitches())):
            if position not in assigned:
                document.clear_footswitch(position)

    for index in range(len(document.snapshots())):
        snapshot = tone.get(f"snapshot{index}")
        if not isinstance(snapshot, dict):
            continue
        states: dict[int, bool] = {}
        for dsp_name, blocks in (snapshot.get("blocks") or {}).items():
            if not isinstance(blocks, dict):
                continue
            for block_id, on in blocks.items():
                target = placed.get(f"{dsp_name}.{block_id}")
                if target is not None:
                    states[target] = bool(on)
        if document.set_snapshot(
            index,
            name=snapshot.get("@name"),
            tempo=snapshot.get("@tempo"),
            valid=snapshot.get("@valid"),
            block_states=states,
        ):
            report.snapshots.append(str(snapshot.get("@name") or index))

    _apply_snapshot_controllers(document, tone, targets, catalog, report)

    current = tone.get("global", {}).get("@current_snapshot")
    if isinstance(current, int) and not isinstance(current, bool):
        document.set_current_snapshot(current)

    if not document.modified:
        return

    session.push_document(
        dump(document), slot=slot, name=name, bank=bank, donor=raw, verify=False
    )


def _apply_snapshot_controllers(
    document: Any,
    tone: dict,
    targets: dict[str, ControllerTarget],
    catalog: Any,
    report: EditReport,
) -> None:
    """Put the tone's snapshot-controlled parameters under snapshot control.

    Without this every snapshot recalls the same knob positions and only its
    on/off states differ. Each ``tone.controller`` entry with ``@controller: 9``
    becomes a device assignment, and each snapshot's ``controllers`` value its
    per-snapshot value.

    The device refuses a value of the wrong type, and the ``.hlx`` does not say
    which type a parameter is. The document read back after the edits does: it
    holds the block's own value at that ordinal in the device's type, so every
    snapshot value and the range are converted to match it.
    """
    from helixgen.device.document import CONTROLLER_SNAPSHOT

    # The donor's assignments name blocks by slot; left in place they would drive
    # whatever the new tone put there.
    document.clear_controllers()
    snapshot_count = len(document.snapshots())

    for dsp_name, blocks in (tone.get("controller") or {}).items():
        if not isinstance(blocks, dict):
            continue
        for block_id, parameters in blocks.items():
            if not isinstance(parameters, dict):
                continue
            for key, assignment in parameters.items():
                label = f"{dsp_name}.{block_id}.{key}"
                target = targets.get(f"{dsp_name}.{block_id}")
                if (
                    target is None
                    or not isinstance(assignment, dict)
                    or assignment.get("@controller") != CONTROLLER_SNAPSHOT
                ):
                    report.unsupported_controllers.append(label)
                    continue

                ordinal = target.symbol.ordinal_of(key)
                stored = document.values(target.slot, cab=target.model_sel == MODEL_PAIRED)
                if ordinal is None or stored is None or ordinal >= len(stored):
                    report.unsupported_controllers.append(label)
                    continue
                kind = type(stored[ordinal])
                model_name = str(target.block.get("@model", ""))
                values = []
                for index in range(snapshot_count):
                    snapshot = tone.get(f"snapshot{index}") or {}
                    entry = (
                        (snapshot.get("controllers") or {})
                        .get(dsp_name, {})
                        .get(block_id, {})
                        .get(key)
                    )
                    raw = entry.get("@value") if isinstance(entry, dict) else None
                    values.append(
                        stored[ordinal]
                        if raw is None
                        else _as_wire_type(raw, kind, catalog, model_name, key)
                    )
                minimum = _as_wire_type(assignment.get("@min"), kind, catalog, model_name, key)
                maximum = _as_wire_type(assignment.get("@max"), kind, catalog, model_name, key)
                if minimum is None or maximum is None or any(v is None for v in values):
                    report.unsupported_controllers.append(label)
                    continue

                assigned = document.add_snapshot_controller(
                    slot=target.slot,
                    parameter=ordinal,
                    model_sel=target.model_sel,
                    minimum=minimum,
                    maximum=maximum,
                    values=values,
                )
                if assigned is None:
                    report.unsupported_controllers.append(label)
                else:
                    report.snapshot_controls.append(label)


def _as_wire_type(
    raw: Any, kind: type, catalog: Any, model_name: str, parameter: str
) -> Any | None:
    """Convert ``raw`` to the device's type for a parameter, or None if it has none.

    An enum named by its label resolves through the catalog, as block values do.
    """
    if isinstance(raw, str):
        raw = enum_index(catalog, model_name, parameter, raw)
    if raw is None:
        return None
    try:
        if kind is bool:
            return bool(raw)
        if kind is int:
            number = float(raw)
            return int(number) if number.is_integer() else None
        if kind is float:
            return float(raw)
    except (TypeError, ValueError):
        return None
    return None
