"""Document-layer tests, built on the real blob shape.

A document is ``magic ⧺ header ⧺ preset-map``, where the header is an **offset
table** the device seeks by. These fixtures reproduce that shape, so the tests
exercise what actually goes on the wire -- in particular the rule that editing
the map invalidates the table and it has to be rebuilt.
"""

from __future__ import annotations

import struct

import msgpack
import pytest

from hlxgen.device.document import (
    KEY_CONTENT,
    KEY_ENABLED,
    KEY_MODEL_INDEX,
    KEY_MODEL_REF,
    KEY_SLOT_ARRAY,
    KEY_TAG,
    KEY_VALUES,
    KEY_VECTOR,
    MAGIC,
    NO_CAB,
    TAG_BLOCK,
    TAG_EMPTY,
    TAG_INPUT,
    TAG_OUTPUT,
    TAG_ROUTING,
    DocumentError,
    _entry_offsets,
    dump,
    parse,
)

HEADER_LEN = 48


def _block(model: int, values: list[float]) -> dict:
    return {
        KEY_TAG: TAG_BLOCK,
        KEY_CONTENT: {
            KEY_MODEL_REF: {23: False, KEY_MODEL_INDEX: model, 26: NO_CAB},
            9: 1,
            KEY_ENABLED: True,
            KEY_VALUES: {2: len(values), 3: len(values), KEY_VECTOR: values},
            12: {2: 0, 3: 0, KEY_VECTOR: []},
        },
    }


def _empty() -> dict:
    return {KEY_TAG: TAG_EMPTY, KEY_CONTENT: None}


def _node(tag: int) -> dict:
    return {KEY_TAG: tag, KEY_CONTENT: {}}


def _slot_array() -> list:
    """Twenty slots laid out as the device does: blocks, nodes, then path two."""
    array: list = [{KEY_TAG: 0, KEY_CONTENT: None}]
    array += [_block(103, [0.1, 0.2, 0.3]), _block(15, [0.5] * 12)]
    array += [_empty() for _ in range(6)]
    array += [_node(TAG_INPUT), _node(TAG_OUTPUT)]
    array += [_empty() for _ in range(8)]
    array += [_node(TAG_ROUTING)]
    return array[:20]


def _footswitch(block_slot: int, label: str) -> list:
    return [
        {
            10: 0,
            11: {0: 1, 2: 0, 5: label.encode() + b"\x00", 6: 13676288, 7: True, 8: block_slot},
            12: False,
            13: False,
            14: b"\x00",
            15: False,
            16: 0,
        }
    ]


def _snapshot(name: str) -> dict:
    return {
        0: True,
        3: [[None, True] for _ in range(20)],
        4: name.encode() + b"\x00",
        5: 120.0,
    }


def _preset(*, footswitches: bool = True) -> dict:
    return {
        0: {KEY_SLOT_ARRAY: _slot_array(), 21: "A"},
        1: None,
        3: {
            7: 2,
            8: (
                [_footswitch(1, "Fuzz"), None, None, None, None]
                if footswitches
                else [None] * 5
            ),
        },
        5: {i: i for i in range(6)},
        7: {35: 0x03800000},
        10: {10: [_snapshot("SNAPSHOT 1"), _snapshot("SNAPSHOT 2")], 13: [True] * 20},
    }


def _document(header_len: int = HEADER_LEN, *, footswitches: bool = True) -> bytes:
    """Assemble a blob carrying a correct offset table."""
    preset = _preset(footswitches=footswitches)
    out = bytearray()
    out += bytes([0xA0 | len(MAGIC)]) + MAGIC
    header_at = len(out) + 3
    out += bytes([0xDA]) + header_len.to_bytes(2, "big")
    out += b"\x00" * header_len
    map_at = len(out)
    out += msgpack.packb(preset, use_bin_type=False, use_single_float=True)

    offsets = _entry_offsets(bytes(out), map_at, preset)
    table = bytearray(struct.pack("<I", map_at))
    for key in preset:
        table += struct.pack("<I", offsets[key])
    while len(table) < header_len:
        table += struct.pack("<I", len(out))
    out[header_at : header_at + header_len] = table[:header_len]
    return bytes(out)


class TestParse:
    def test_reads_magic_header_and_map(self):
        doc = parse(_document())
        assert doc.magic == MAGIC
        assert doc.header_len == HEADER_LEN
        assert sorted(doc.preset) == [0, 1, 3, 5, 7, 10]

    def test_a_fragment_without_the_magic_is_rejected(self):
        """A stream reassembled without its first page looks exactly like this.

        It is the failure that silently produced wrong backups, so it has to be
        an error rather than a document that merely looks odd.
        """
        with pytest.raises(DocumentError, match="l6-helix magic"):
            parse(_document()[300:])

    def test_empty_document_is_rejected(self):
        with pytest.raises(DocumentError, match="empty document"):
            parse(b"")

    def test_header_slots_are_classified_not_copied(self):
        doc = parse(_document())
        kinds = [slot.kind for slot in doc.header_slots]
        assert kinds[0] == "map"
        assert "key" in kinds
        assert kinds[-1] == "total"


class TestOffsetTable:
    def test_round_trip_preserves_the_preset(self):
        doc = parse(_document())
        assert parse(dump(doc)).preset == doc.preset

    def test_key_offsets_are_rebuilt_against_the_new_bytes(self):
        """The device seeks by these; a stale table points into the middle of values."""
        doc = parse(_document())
        doc.set_values(1, [0.9] * 8)
        blob = dump(doc)

        again = parse(blob)
        unpacker = msgpack.Unpacker(None, strict_map_key=False, raw=True)
        unpacker.feed(blob)
        unpacker.unpack()
        unpacker.unpack()
        map_at = unpacker.tell()
        actual = _entry_offsets(blob, map_at, again.preset)

        for index, slot in enumerate(again.header_slots):
            if slot.kind == "key":
                (stored,) = struct.unpack_from("<I", blob, 13 + 4 * index)
                assert stored == actual[slot.value]

    def test_the_header_keeps_its_length(self):
        doc = parse(_document())
        doc.set_values(1, [0.1] * 20)
        assert parse(dump(doc)).header_len == HEADER_LEN

    def test_total_length_slot_tracks_the_blob(self):
        doc = parse(_document())
        doc.set_values(1, [0.1] * 30)
        blob = dump(doc)
        again = parse(blob)
        for index, slot in enumerate(again.header_slots):
            if slot.kind == "total":
                (stored,) = struct.unpack_from("<I", blob, 13 + 4 * index)
                assert stored == len(blob)


class TestAddressing:
    def test_position_maps_to_slot_index_plus_one(self):
        doc = parse(_document())
        assert doc.record_for(0, 0) == 1
        assert doc.record_for(0, 1) == 2

    def test_second_path_starts_ten_slots_in(self):
        doc = parse(_document())
        assert doc.record_for(1, 0) == 11

    def test_out_of_range_is_none(self):
        doc = parse(_document())
        assert doc.record_for(0, 99) is None
        assert doc.record_for(9, 0) is None


class TestBlocks:
    def test_blocks_skip_empty_slots_and_nodes(self):
        assert parse(_document()).block_slots() == [1, 2]

    def test_model_index(self):
        doc = parse(_document())
        assert doc.model_index(1) == 103
        assert doc.model_index(2) == 15

    def test_no_cab_reads_as_none_not_minus_one(self):
        assert parse(_document()).cab_index(1) is None

    def test_values(self):
        assert parse(_document()).values(1) == pytest.approx([0.1, 0.2, 0.3])

    def test_empty_slot_detection(self):
        doc = parse(_document())
        assert doc.is_empty_slot(3)
        assert not doc.is_empty_slot(1)


class TestMutation:
    def test_set_values_updates_both_counts(self):
        doc = parse(_document())
        doc.set_values(1, [0.4, 0.4])
        vector = doc._slot(0, 1)[KEY_CONTENT][KEY_VALUES]
        assert vector[2] == 2
        assert vector[3] == 2

    def test_set_model_index(self):
        doc = parse(_document())
        doc.set_model_index(1, 246)
        assert doc.model_index(1) == 246
        assert doc.modified

    def test_set_enabled(self):
        doc = parse(_document())
        doc.set_enabled(1, False)
        assert doc._slot(0, 1)[KEY_CONTENT][KEY_ENABLED] is False

    def test_materialize_shapes_an_empty_slot_from_a_real_block(self):
        doc = parse(_document())
        doc.materialize_slot(3, 1)
        assert doc.model_index(3) == 103
        assert doc.values(3) == pytest.approx([0.1, 0.2, 0.3])

    def test_materialize_refuses_a_non_block_template(self):
        doc = parse(_document())
        with pytest.raises(DocumentError, match="not a block"):
            doc.materialize_slot(3, 4)

    def test_a_materialized_slot_is_independent_of_its_template(self):
        doc = parse(_document())
        doc.materialize_slot(3, 1)
        doc.set_values(3, [0.7])
        assert doc.values(1) == pytest.approx([0.1, 0.2, 0.3])


class TestFootswitches:
    def test_the_array_is_one_entry_per_switch(self):
        assert len(parse(_document()).footswitches()) == 5

    def test_binding_sets_the_block_slot_not_the_switch_number(self):
        """The switch is the entry's position; the block it drives is a field.

        The two are independent -- real presets routinely bind switch 1 to a
        block deep in the chain.
        """
        doc = parse(_document())
        assert doc.set_footswitch(2, block_slot=6, label="Delay")

        entry = doc.footswitches()[2]
        assert entry[0][11][8] == 6
        assert entry[0][11][5] == b"Delay\x00"

    def test_the_label_is_nul_terminated(self):
        doc = parse(_document())
        doc.set_footswitch(0, block_slot=1, label="Comp")
        assert doc.footswitches()[0][0][11][5].endswith(b"\x00")

    def test_colour_and_enabled_are_carried(self):
        doc = parse(_document())
        doc.set_footswitch(0, block_slot=1, label="X", color=65408, enabled=False)
        inner = doc.footswitches()[0][0][11]
        assert inner[6] == 65408
        assert inner[7] is False

    def test_an_empty_slot_is_shaped_from_a_populated_sibling(self):
        doc = parse(_document())
        doc.set_footswitch(3, block_slot=2, label="New")
        # The undecoded outer fields come from the device's own entry.
        assert doc.footswitches()[3][0][14] == b"\x00"
        assert doc.footswitches()[3][0][12] is False

    def test_a_wholly_empty_layout_still_binds(self):
        """A document rebuilt by writes has no entry to clone from."""
        doc = parse(_document(footswitches=False))
        assert doc.set_footswitch(0, block_slot=4, label="Verb")
        assert doc.footswitches()[0][0][11][8] == 4

    def test_out_of_range_switch_is_refused(self):
        doc = parse(_document())
        assert not doc.set_footswitch(99, block_slot=1, label="X")

    def test_clearing_leaves_the_switch_unassigned(self):
        doc = parse(_document())
        doc.clear_footswitch(0)
        assert doc.footswitches()[0] is None

    def test_binding_survives_a_round_trip(self):
        doc = parse(_document())
        doc.set_footswitch(1, block_slot=5, label="Trem")
        again = parse(dump(doc))
        assert again.footswitches()[1][0][11][8] == 5


class TestSnapshots:
    def test_name_tempo_and_valid(self):
        doc = parse(_document())
        assert doc.set_snapshot(0, name="Verse", tempo=96.0, valid=True)

        snapshot = doc.snapshots()[0]
        assert snapshot[4] == b"Verse\x00"
        assert snapshot[5] == pytest.approx(96.0)
        assert snapshot[0] is True

    def test_block_states_land_at_slot_index_one(self):
        """The device stores a block's per-snapshot state at ``3[slot][1]``."""
        doc = parse(_document())
        doc.set_snapshot(0, block_states={2: False, 3: True})

        slots = doc.snapshots()[0][3]
        assert slots[2][1] is False
        assert slots[3][1] is True

    def test_other_slots_are_untouched(self):
        doc = parse(_document())
        before = list(doc.snapshots()[0][3][7])
        doc.set_snapshot(0, block_states={1: False})
        assert doc.snapshots()[0][3][7] == before

    def test_out_of_range_snapshot_is_refused(self):
        assert not parse(_document()).set_snapshot(9, name="X")

    def test_survives_a_round_trip(self):
        doc = parse(_document())
        doc.set_snapshot(1, name="Chorus", tempo=140.0)
        again = parse(dump(doc))
        assert again.snapshots()[1][4] == b"Chorus\x00"
        assert again.snapshots()[1][5] == pytest.approx(140.0)
