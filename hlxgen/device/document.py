"""The device's preset document.

A document read off the pedal is three MessagePack values back to back::

    "l6-helix\\0"        magic, a 9-byte fixstr
    <48-byte str>       the header -- an offset table, see below
    {0: ..., 1: ...}    the preset map

**The header is an offset table, not opaque bytes.** It is twelve little-endian
u32 slots: where the preset map starts, the byte offset of each of the map's
top-level entries, and the blob's total length. The device uses it to find fields
without walking the map, so **any edit to the map invalidates it** and it has to
be recomputed. Writing a document back with a stale table is writing a document
the device cannot read -- it accepts the transfer and stores an empty preset.

The slots are classified on parse -- each u32 is recognised as the map start, the
total length, the offset of a particular key, or a constant -- so the same table
can be rebuilt after the map changes, at the same fixed length.

Blocks live at ``preset[<dsp group>][22]``, a 20-entry array per DSP where a slot
is either a block (``{19: 6, 20: {...}}``) or empty (``{19: 8, 20: nil}``), with
the input, output and routing nodes at fixed positions inside it. A tone's
``@path``/``@position`` addresses it as ``path * 10 + position + 1``.

Inside a block's content:

=========  ==================================================================
key        meaning
=========  ==================================================================
``24``     model reference: ``25`` = index into ``Helix.sym``, ``26`` = paired
           cab index (``-1`` when the block has none)
``9``      block type
``10``     enabled
``11``     main value vector, as ``{2: n, 3: n, 4: [...]}``
``12``     the paired cab's value vector, same shape
=========  ==================================================================
"""

from __future__ import annotations

import struct
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final

import msgpack

MAGIC: Final = b"l6-helix\x00"

#: Record tags.
TAG_BLOCK: Final = 6
TAG_EMPTY: Final = 8
TAG_INPUT: Final = 1
TAG_OUTPUT: Final = 2
TAG_ROUTING: Final = 3

KEY_TAG: Final = 19
KEY_CONTENT: Final = 20

#: Where a DSP group keeps its slot array.
KEY_SLOT_ARRAY: Final = 22

#: Block content keys.
KEY_MODEL_REF: Final = 24
KEY_TYPE: Final = 9
KEY_ENABLED: Final = 10
KEY_VALUES: Final = 11
KEY_CAB_VALUES: Final = 12

KEY_MODEL_INDEX: Final = 25
KEY_CAB_INDEX: Final = 26
KEY_MODEL_FLAG: Final = 23

#: Value-vector keys. ``2`` and ``3`` are both the count; ``4`` is the list.
KEY_COUNT_A: Final = 2
KEY_COUNT_B: Final = 3
KEY_VECTOR: Final = 4

NO_CAB: Final = -1

#: Where the footswitch layout lives: ``preset[3][8]``, one entry per switch.
KEY_FOOTSWITCH_GROUP: Final = 3
KEY_FOOTSWITCH_ARRAY: Final = 8
#: Inside a footswitch entry's ``11`` sub-map.
KEY_FS_LABEL: Final = 5
KEY_FS_COLOR: Final = 6
KEY_FS_ENABLED: Final = 7
#: **The block slot the switch controls.** Not the switch number -- the switch is
#: the entry's position in the array.
KEY_FS_BLOCK: Final = 8
KEY_FS_INNER: Final = 11

#: Where snapshots live: ``preset[10][10]``.
KEY_SNAPSHOT_GROUP: Final = 10
KEY_SNAPSHOT_ARRAY: Final = 10
KEY_SNAP_VALID: Final = 0
KEY_SNAP_BLOCKS: Final = 3
KEY_SNAP_NAME: Final = 4
KEY_SNAP_TEMPO: Final = 5
#: The snapshot group's current-snapshot index: ``preset[10][6]``.
KEY_CURRENT_SNAPSHOT: Final = 6
#: How many controller assignments the preset holds, across every source:
#: ``preset[10][8]``. Equal to the count in ``preset[4]`` on all 126 presets read
#: off an HX Stomp, and **load-bearing**: with it left at 0 the device shows the
#: assignments but never recalls a snapshot's values.
KEY_CONTROLLER_COUNT: Final = 8
#: A snapshot's controller values: 64 entries of ``[fs_enabled, id, value]``,
#: where ``id`` is the controller assignment the value belongs to.
KEY_SNAP_CONTROLLERS: Final = 2
#: An unused controller-value entry. Stale entries whose assignment is gone keep
#: an old value and an id of 13 or 64; the device treats all of them as unused.
UNUSED_CONTROLLER_VALUE: Final = (False, 64, None)

#: Controller assignments: ``preset[4]``, a list indexed by controller source
#: (1 EXP 1, 2 EXP 2, 9 Snapshots, ...), each a list of assignments or nil.
#: Assignment ids are shared across sources and index every snapshot's
#: ``KEY_SNAP_CONTROLLERS`` list.
KEY_CONTROLLER_GROUP: Final = 4
CONTROLLER_SNAPSHOT: Final = 9
#: Inside an assignment: ``{0: id, 1: {...}}``, and the inner map's fields.
KEY_CTRL_ID: Final = 0
KEY_CTRL_BODY: Final = 1
KEY_CTRL_SOURCE: Final = 0
KEY_CTRL_MIN: Final = 2
KEY_CTRL_MAX: Final = 3
KEY_CTRL_SLOT: Final = 5
KEY_CTRL_TARGET: Final = 6
KEY_CTRL_MODEL_SEL: Final = 7
#: Inside the target sub-map: which sub-model, and the parameter ordinal.
KEY_TARGET_MODEL_SEL: Final = 28
KEY_TARGET_PARAM: Final = 29

#: Shape of a block-bypass footswitch entry, for a document whose layout is
#: empty and so offers nothing to clone. Taken from 336 real entries read off an
#: HX Stomp: 330 share exactly this, and the six that differ carry an extra
#: sub-map at ``11 -> 9`` because they assign a *parameter* rather than a block's
#: bypass, which is not what this writes.
FOOTSWITCH_TEMPLATE: Final = {
    10: 0,
    11: {0: 1, 2: 0, 5: b"\x00", 6: 0, 7: True, 8: 0},
    12: False,
    13: False,
    14: b"\x00",
    15: False,
    16: 0,
}

#: Slots per DSP group, and the stride a tone's ``@path`` advances by.
SLOTS_PER_DSP: Final = 20
PATH_STRIDE: Final = 10


class DocumentError(RuntimeError):
    """Raised when a device document cannot be parsed or re-encoded."""


@dataclass(frozen=True)
class HeaderSlot:
    """One u32 of the header's offset table."""

    #: ``"map"``, ``"total"``, ``"key"`` or ``"raw"``.
    kind: str
    #: The map key, for ``kind == "key"``; the literal value for ``"raw"``.
    value: int = 0


@dataclass
class Document:
    """A parsed device preset document."""

    magic: bytes
    header_slots: list[HeaderSlot]
    header_len: int
    preset: dict
    raw: bytes = b""
    dirty: bool = False

    # -- addressing --------------------------------------------------------

    def dsp_groups(self) -> list[int]:
        """Top-level keys that carry a slot array, in order."""
        return [
            key
            for key in sorted(k for k in self.preset if isinstance(k, int))
            if isinstance(self.preset.get(key), dict)
            and isinstance(self.preset[key].get(KEY_SLOT_ARRAY), list)
        ]

    def slot_array(self, path: int = 0) -> list | None:
        """The slot array a tone's ``@path`` addresses.

        One DSP group holds twenty slots and therefore two paths of ten, which is
        why the group is chosen by ``path * 10 // 20`` rather than by ``path``.
        """
        groups = self.dsp_groups()
        if not groups:
            return None
        group_index = path * PATH_STRIDE // SLOTS_PER_DSP
        if group_index >= len(groups):
            return None
        return self.preset[groups[group_index]][KEY_SLOT_ARRAY]

    def record_for(self, path: int, position: int) -> int | None:
        """Index into the slot array for ``@path``/``@position``."""
        array = self.slot_array(path)
        if array is None:
            return None
        # A path owns ten slots. Without this bound the modulo below silently
        # wraps an out-of-range position onto a real slot in the same array,
        # which would overwrite an unrelated block instead of reporting the
        # tone as unplaceable.
        if position < 0 or position >= PATH_STRIDE:
            return None
        index = (path * PATH_STRIDE + position + 1) % SLOTS_PER_DSP
        if index >= len(array):
            return None
        return index

    def _slot(self, path: int, index: int) -> Any:
        array = self.slot_array(path)
        return None if array is None else array[index]

    # -- blocks ------------------------------------------------------------

    def blocks(self, path: int = 0) -> list[tuple[int, dict]]:
        """Every block in a path's slot array, as ``(index, content)``."""
        array = self.slot_array(path) or []
        return [
            (index, entry[KEY_CONTENT])
            for index, entry in enumerate(array)
            if isinstance(entry, dict)
            and entry.get(KEY_TAG) == TAG_BLOCK
            and isinstance(entry.get(KEY_CONTENT), dict)
        ]

    def block_slots(self, path: int = 0) -> list[int]:
        return [index for index, _ in self.blocks(path)]

    def first_block(self, path: int = 0) -> int | None:
        slots = self.block_slots(path)
        return slots[0] if slots else None

    def is_empty_slot(self, index: int, path: int = 0) -> bool:
        entry = self._slot(path, index)
        return isinstance(entry, dict) and entry.get(KEY_TAG) == TAG_EMPTY

    def materialize_slot(self, index: int, template: int, path: int = 0) -> None:
        """Turn an empty slot into a block, shaped like an existing one.

        A block record carries fields this project has not decoded, so an empty
        slot is filled by deep-copying one the **device itself wrote** rather
        than by inventing a record.
        """
        array = self.slot_array(path)
        if array is None:
            raise DocumentError(f"no slot array for path {path}")
        source = array[template]
        if not isinstance(source, dict) or source.get(KEY_TAG) != TAG_BLOCK:
            raise DocumentError(f"slot {template} is not a block and cannot be a template")
        array[index] = deepcopy(source)
        self.dirty = True

    def model_index(self, index: int, path: int = 0) -> int | None:
        entry = self._slot(path, index)
        if not isinstance(entry, dict) or not isinstance(entry.get(KEY_CONTENT), dict):
            return None
        ref = entry[KEY_CONTENT].get(KEY_MODEL_REF)
        return ref.get(KEY_MODEL_INDEX) if isinstance(ref, dict) else None

    def cab_index(self, index: int, path: int = 0) -> int | None:
        entry = self._slot(path, index)
        if not isinstance(entry, dict) or not isinstance(entry.get(KEY_CONTENT), dict):
            return None
        ref = entry[KEY_CONTENT].get(KEY_MODEL_REF)
        if not isinstance(ref, dict):
            return None
        value = ref.get(KEY_CAB_INDEX, NO_CAB)
        return None if value == NO_CAB else value

    def values(self, index: int, path: int = 0, *, cab: bool = False) -> list | None:
        entry = self._slot(path, index)
        if not isinstance(entry, dict) or not isinstance(entry.get(KEY_CONTENT), dict):
            return None
        vector = entry[KEY_CONTENT].get(KEY_CAB_VALUES if cab else KEY_VALUES)
        if not isinstance(vector, dict):
            return None
        out = vector.get(KEY_VECTOR)
        return out if isinstance(out, list) else None

    def set_values(
        self, index: int, values: list, path: int = 0, *, cab: bool = False
    ) -> None:
        """Replace a block's value vector, keeping both count keys consistent.

        The device stores the length twice (keys 2 and 3); writing one without
        the other produces a document that reads back wrong.
        """
        content = self._slot(path, index)[KEY_CONTENT]
        vector = content.setdefault(KEY_CAB_VALUES if cab else KEY_VALUES, {})
        vector[KEY_COUNT_A] = len(values)
        vector[KEY_COUNT_B] = len(values)
        vector[KEY_VECTOR] = list(values)
        self.dirty = True

    def set_model_index(self, index: int, model: int, path: int = 0) -> None:
        ref = self._slot(path, index)[KEY_CONTENT].setdefault(KEY_MODEL_REF, {})
        ref[KEY_MODEL_INDEX] = model
        self.dirty = True

    def set_enabled(self, index: int, enabled: bool, path: int = 0) -> None:
        self._slot(path, index)[KEY_CONTENT][KEY_ENABLED] = bool(enabled)
        self.dirty = True

    # -- footswitches ------------------------------------------------------

    def footswitches(self) -> list:
        """The footswitch array, one entry per physical switch."""
        group = self.preset.get(KEY_FOOTSWITCH_GROUP)
        if not isinstance(group, dict):
            return []
        array = group.get(KEY_FOOTSWITCH_ARRAY)
        return array if isinstance(array, list) else []

    def set_footswitch(
        self,
        switch: int,
        *,
        block_slot: int,
        label: str,
        color: int | None = None,
        enabled: bool = True,
    ) -> bool:
        """Bind a physical switch to a block.

        ``switch`` is zero-based -- it is the entry's **position** in the array,
        which is what identifies the switch. The block it controls is a field
        *inside* the entry (``11 -> 8``), so the two are independent: switch 1 can
        drive block 6, and on real presets frequently does.

        The entry's undecoded fields are kept by cloning a populated entry from
        the same document where one exists, rather than inventing a record.
        """
        array = self.footswitches()
        if switch < 0 or switch >= len(array):
            return False

        entry = array[switch]
        if not (isinstance(entry, list) and entry and isinstance(entry[0], dict)):
            # Prefer a populated entry from this very document, so the
            # undecoded fields stay the device's own. A document whose layout is
            # empty offers none, and then the measured template stands in.
            donor = next(
                (
                    e
                    for e in array
                    if isinstance(e, list) and e and isinstance(e[0], dict)
                ),
                None,
            )
            entry = deepcopy(donor) if donor is not None else [deepcopy(FOOTSWITCH_TEMPLATE)]
            array[switch] = entry

        inner = entry[0].setdefault(KEY_FS_INNER, {})
        inner[KEY_FS_BLOCK] = block_slot
        inner[KEY_FS_LABEL] = label.encode("ascii", "replace") + b"\x00"
        inner[KEY_FS_ENABLED] = bool(enabled)
        if color is not None:
            inner[KEY_FS_COLOR] = color
        self.dirty = True
        return True

    def clear_footswitch(self, switch: int) -> None:
        """Leave a switch unassigned."""
        array = self.footswitches()
        if 0 <= switch < len(array):
            array[switch] = None
            self.dirty = True

    # -- snapshots ---------------------------------------------------------

    def snapshots(self) -> list:
        """The snapshot array."""
        group = self.preset.get(KEY_SNAPSHOT_GROUP)
        if not isinstance(group, dict):
            return []
        array = group.get(KEY_SNAPSHOT_ARRAY)
        return array if isinstance(array, list) else []

    def set_snapshot(
        self,
        index: int,
        *,
        name: str | None = None,
        tempo: float | None = None,
        valid: bool | None = None,
        block_states: dict[int, bool] | None = None,
    ) -> bool:
        """Update one snapshot.

        ``block_states`` maps a block's **slot index** to whether it is on in this
        snapshot; the device stores that at ``3[slot][1]``, leaving the rest of
        each per-slot entry alone.
        """
        array = self.snapshots()
        if index < 0 or index >= len(array):
            return False
        snapshot = array[index]
        if not isinstance(snapshot, dict):
            return False

        if name is not None:
            snapshot[KEY_SNAP_NAME] = name.encode("ascii", "replace") + b"\x00"
        if tempo is not None:
            snapshot[KEY_SNAP_TEMPO] = float(tempo)
        if valid is not None:
            snapshot[KEY_SNAP_VALID] = bool(valid)

        if block_states:
            slots = snapshot.get(KEY_SNAP_BLOCKS)
            if isinstance(slots, list):
                for slot, on in block_states.items():
                    if (
                        0 <= slot < len(slots)
                        and isinstance(slots[slot], list)
                        and len(slots[slot]) > 1
                    ):
                        slots[slot][1] = bool(on)
        self.dirty = True
        return True

    def set_current_snapshot(self, index: int) -> bool:
        """Choose the snapshot the preset opens on."""
        group = self.preset.get(KEY_SNAPSHOT_GROUP)
        if not isinstance(group, dict) or not 0 <= index < len(self.snapshots()):
            return False
        group[KEY_CURRENT_SNAPSHOT] = index
        self.dirty = True
        return True

    # -- controllers -------------------------------------------------------

    def controller_assignments(self, source: int) -> list:
        """Assignments for one controller source (e.g. ``CONTROLLER_SNAPSHOT``)."""
        group = self.preset.get(KEY_CONTROLLER_GROUP)
        if not isinstance(group, list) or not 0 <= source < len(group):
            return []
        return group[source] if isinstance(group[source], list) else []

    def clear_controllers(self) -> None:
        """Drop every controller assignment and every snapshot's controller values.

        Assignments address a block by slot, so ones left over from the donor
        would bind to whatever block the new tone put in that slot.
        """
        group = self.preset.get(KEY_CONTROLLER_GROUP)
        if isinstance(group, list):
            for source in range(len(group)):
                group[source] = None
        for snapshot in self.snapshots():
            entries = snapshot.get(KEY_SNAP_CONTROLLERS) if isinstance(snapshot, dict) else None
            if isinstance(entries, list):
                for position in range(len(entries)):
                    entries[position] = list(UNUSED_CONTROLLER_VALUE)
        self._sync_controller_count()
        self.dirty = True

    def _sync_controller_count(self) -> None:
        snapshot_group = self.preset.get(KEY_SNAPSHOT_GROUP)
        if not isinstance(snapshot_group, dict):
            return
        group = self.preset.get(KEY_CONTROLLER_GROUP)
        snapshot_group[KEY_CONTROLLER_COUNT] = sum(
            len(source) for source in group or [] if isinstance(source, list)
        )

    def add_snapshot_controller(
        self,
        *,
        slot: int,
        parameter: int,
        model_sel: int,
        minimum: Any,
        maximum: Any,
        values: list,
    ) -> int | None:
        """Put one parameter under snapshot control, with a value per snapshot.

        The assignment's shape is the one every snapshot assignment on a real HX
        Stomp shares. ``values`` holds one value per snapshot, in the
        parameter's own wire type. Returns the assignment id, or None if the
        document has no room for another controller.
        """
        group = self.preset.get(KEY_CONTROLLER_GROUP)
        snapshots = self.snapshots()
        if not isinstance(group, list) or len(group) <= CONTROLLER_SNAPSHOT:
            return None
        if len(values) != len(snapshots):
            raise DocumentError(
                f"expected {len(snapshots)} snapshot values, got {len(values)}"
            )
        entries = [snapshot.get(KEY_SNAP_CONTROLLERS) for snapshot in snapshots]
        if not all(isinstance(entry, list) for entry in entries):
            return None

        taken = {
            assignment.get(KEY_CTRL_ID)
            for source in group
            if isinstance(source, list)
            for assignment in source
            if isinstance(assignment, dict)
        }
        capacity = min(len(entry) for entry in entries)
        free = next((i for i in range(capacity) if i not in taken), None)
        if free is None:
            return None

        assignment = {
            KEY_CTRL_ID: free,
            KEY_CTRL_BODY: {
                KEY_CTRL_SOURCE: CONTROLLER_SNAPSHOT,
                1: 4,
                KEY_CTRL_MIN: minimum,
                KEY_CTRL_MAX: maximum,
                4: 0,
                KEY_CTRL_SLOT: slot,
                KEY_CTRL_TARGET: {
                    KEY_TARGET_MODEL_SEL: model_sel,
                    KEY_TARGET_PARAM: parameter,
                    41: False,
                },
                KEY_CTRL_MODEL_SEL: model_sel,
                13: False,
            },
        }
        if not isinstance(group[CONTROLLER_SNAPSHOT], list):
            group[CONTROLLER_SNAPSHOT] = []
        group[CONTROLLER_SNAPSHOT].append(assignment)
        for entry, value in zip(entries, values, strict=True):
            entry[free] = [False, free, value]
        self._sync_controller_count()
        self.dirty = True
        return free

    @property
    def modified(self) -> bool:
        return self.dirty


def parse(raw: bytes) -> Document:
    """Parse a document read off the device."""
    if not raw:
        raise DocumentError("empty document")

    unpacker = msgpack.Unpacker(None, strict_map_key=False, raw=True)
    unpacker.feed(raw)
    try:
        magic = unpacker.unpack()
        header = unpacker.unpack()
        map_at = unpacker.tell()
        preset = unpacker.unpack()
    except Exception as exc:
        raise DocumentError(f"not a device document: {exc}") from exc

    if magic != MAGIC:
        raise DocumentError(
            f"document does not start with the l6-helix magic (got {magic[:12]!r}). "
            "A stream reassembled without its first page looks exactly like this."
        )
    if not isinstance(preset, dict):
        raise DocumentError(f"preset is a {type(preset).__name__}, not a map")

    offsets = _entry_offsets(raw, map_at, preset)
    slots = _classify(header, map_at, len(raw), offsets)

    return Document(
        magic=magic,
        header_slots=slots,
        header_len=len(header),
        preset=preset,
        raw=raw,
    )


def dump(document: Document) -> bytes:
    """Re-encode a document, rebuilding the header's offset table.

    The table is recomputed against the bytes actually emitted. That is what
    keeps an edited document readable: the device seeks by those offsets, so a
    table still describing the old layout points into the middle of values.
    """
    out = bytearray()
    out += _str_header(len(document.magic)) + document.magic

    header_at = len(out) + _str_header_len(document.header_len)
    out += _str_header(document.header_len)
    out += b"\x00" * document.header_len

    map_at = len(out)
    out += msgpack.packb(document.preset, use_bin_type=False, use_single_float=True)

    offsets = _entry_offsets(bytes(out), map_at, document.preset)
    table = bytearray()
    for slot in document.header_slots:
        if slot.kind == "map":
            value = map_at
        elif slot.kind == "total":
            value = len(out)
        elif slot.kind == "key":
            value = offsets.get(slot.value, len(out))
        else:
            value = slot.value
        table += struct.pack("<I", value)

    table = table[: document.header_len]
    out[header_at : header_at + len(table)] = table
    return bytes(out)


# -- internals -------------------------------------------------------------


def _str_header_len(length: int) -> int:
    if length < 32:
        return 1
    if length < 65536:
        return 3
    return 5


def _str_header(length: int) -> bytes:
    """A MessagePack ``str`` prefix, forcing ``str16`` as the device does."""
    if length < 32:
        return bytes([0xA0 | length])
    if length < 65536:
        return bytes([0xDA]) + length.to_bytes(2, "big")
    return bytes([0xDB]) + length.to_bytes(4, "big")


def _entry_offsets(blob: bytes, map_at: int, preset: dict) -> dict[int, int]:
    """Byte offset of each top-level entry's **key** -- what the table points at."""
    offsets: dict[int, int] = {}
    body = blob[map_at:]
    if not body:
        return offsets

    marker = body[0]
    if 0x80 <= marker <= 0x8F:
        position = 1
    elif marker == 0xDE:
        position = 3
    elif marker == 0xDF:
        position = 5
    else:
        return offsets

    for key in preset:
        offsets[key] = map_at + position
        position += _value_len(body, position)  # the key
        position += _value_len(body, position)  # its value
    return offsets


def _value_len(body: bytes, position: int) -> int:
    unpacker = msgpack.Unpacker(None, strict_map_key=False, raw=True)
    unpacker.feed(body[position:])
    unpacker.unpack()
    return unpacker.tell()


def _classify(
    header: bytes, map_at: int, total: int, offsets: dict[int, int]
) -> list[HeaderSlot]:
    """Recognise what each u32 of the header points at.

    Classifying rather than copying is what lets the table be rebuilt at the same
    fixed length after the map has moved underneath it.
    """
    slots: list[HeaderSlot] = []
    by_offset = {offset: key for key, offset in offsets.items()}
    for index in range(len(header) // 4):
        (value,) = struct.unpack_from("<I", header, index * 4)
        if value == map_at:
            slots.append(HeaderSlot("map"))
        elif value == total:
            slots.append(HeaderSlot("total"))
        elif value in by_offset:
            slots.append(HeaderSlot("key", by_offset[value]))
        else:
            slots.append(HeaderSlot("raw", value))
    return slots
