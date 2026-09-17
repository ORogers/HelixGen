"""Codec tests -- the donor overlay.

Built on synthetic symbol tables and synthetic documents, so none of this needs
a pedal or any Line 6 data. The rules under test are the ones that are easy to
implement backwards and expensive to get wrong.
"""

from __future__ import annotations

import struct

import msgpack
import pytest

from hlxgen.device.codec import (
    CodecError,
    overlay,
    record_index_for,
    tone_blocks,
)
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
    Document,
    _entry_offsets,
    parse,
)
from hlxgen.device.symbols import DeviceSymbols


def _symbols() -> DeviceSymbols:
    """A table with a both-variant model, a stereo-only one, and a cab."""
    # HD2_Chorus is deliberately split Mono/Stereo with Spread mid-list, which is
    # the shape that shifts every ordinal after it.
    return DeviceSymbols.parse(
        """[
          {"symbol": "HD2_AmpFoo",        "parameters": ["Drive", "Bass", "Mid"]},
          {"symbol": "HD2_ChorusMono",    "parameters": ["Rate", "Depth", "Mix"]},
          {"symbol": "HD2_ChorusStereo",  "parameters": ["Rate", "Depth", "Spread", "Mix"]},
          {"symbol": "HD2_VerbStereo",    "parameters": ["Decay", "Mix"]},
          {"symbol": "HD2_CabX",          "parameters": ["Position", "Level", "IrData"]}
        ]"""
    )


def _slot_array() -> list:
    """Twenty slots: one block, then empties, the nodes, and path two."""
    array: list = [{KEY_TAG: 0, KEY_CONTENT: None}]
    array.append(_block(0, [0.1, 0.2, 0.3]))
    array += [_empty() for _ in range(7)]
    array += [_node(1), _node(2)]
    array += [_empty() for _ in range(8)]
    array += [_node(3)]
    return array[:20]


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


HEADER_LEN = 48


def _build(preset: dict) -> bytes:
    """Assemble a blob with a valid offset table, as the device writes one."""
    out = bytearray()
    out += bytes([0xA0 | len(MAGIC)]) + MAGIC
    header_at = len(out) + 3
    out += bytes([0xDA]) + HEADER_LEN.to_bytes(2, "big")
    out += b"\x00" * HEADER_LEN
    map_at = len(out)
    out += msgpack.packb(preset, use_bin_type=False, use_single_float=True)

    offsets = _entry_offsets(bytes(out), map_at, preset)
    table = bytearray(struct.pack("<I", map_at))
    for key in preset:
        table += struct.pack("<I", offsets[key])
    while len(table) < HEADER_LEN:
        table += struct.pack("<I", len(out))
    out[header_at : header_at + HEADER_LEN] = table[:HEADER_LEN]
    return bytes(out)


def _donor() -> Document:
    """A parsed donor, built through the real framing so its table is valid."""
    return parse(_build({0: {KEY_SLOT_ARRAY: _slot_array()}, 7: {35: 1}}))


def _preset(blocks: list[dict]) -> dict:
    dsp = {f"block{i}": b for i, b in enumerate(blocks)}
    dsp["split"] = {"@model": "HD2_AppDSPFlowSplitY", "@position": 0}
    return {"data": {"tone": {"dsp0": dsp}}}


class TestToneBlocks:
    def test_routing_nodes_are_not_blocks(self):
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 0}])
        assert [n for n, _ in tone_blocks(preset)] == ["dsp0.block0"]

    def test_blocks_come_back_in_order(self):
        preset = _preset(
            [
                {"@model": "HD2_AmpFoo", "@position": 0},
                {"@model": "HD2_VerbStereo", "@position": 1},
            ]
        )
        assert [n for n, _ in tone_blocks(preset)] == ["dsp0.block0", "dsp0.block1"]

    def test_a_preset_with_no_tone_yields_nothing(self):
        assert tone_blocks({"data": {}}) == []


class TestSlotMapping:
    def test_position_maps_into_the_donors_slot_array(self):
        donor = _donor()
        assert record_index_for(donor, 0, 0) == 1
        assert record_index_for(donor, 0, 2) == 3

    def test_second_path_starts_ten_slots_in(self):
        donor = _donor()
        assert record_index_for(donor, 1, 0) == 11

    def test_out_of_range_position_is_none_not_a_wrong_slot(self):
        """Out of range must not wrap onto an unrelated block."""
        donor = _donor()
        assert record_index_for(donor, 0, 99) is None
        assert record_index_for(donor, 7, 0) is None


class TestVariantResolution:
    def test_absent_stereo_flag_picks_the_only_variant(self):
        """A stereo-only model must not be read as Mono."""
        donor = _donor()
        preset = _preset([{"@model": "HD2_VerbStereo", "@position": 0}])
        report = overlay(donor, preset, _symbols())
        assert report.blocks[0].symbol == "HD2_VerbStereo"

    def test_stereo_flag_selects_between_real_variants(self):
        symbols = _symbols()
        donor = _donor()
        report = overlay(
            donor, _preset([{"@model": "HD2_Chorus", "@position": 0, "@stereo": True}]), symbols
        )
        assert report.blocks[0].symbol == "HD2_ChorusStereo"

        donor = _donor()
        report = overlay(
            donor, _preset([{"@model": "HD2_Chorus", "@position": 0, "@stereo": False}]), symbols
        )
        assert report.blocks[0].symbol == "HD2_ChorusMono"

    def test_unknown_model_is_an_error_not_a_guess(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_NotAThing", "@position": 0}])
        with pytest.raises(CodecError, match="no device symbol"):
            overlay(donor, preset, _symbols())


class TestValueVector:
    def test_values_land_at_their_device_ordinals(self):
        donor = _donor()
        preset = _preset(
            [{"@model": "HD2_AmpFoo", "@position": 0, "Mid": 0.9, "Drive": 0.1}]
        )
        overlay(donor, preset, _symbols())
        assert donor.values(1) == pytest.approx([0.1, 0.2, 0.9])  # Bass kept

    def test_a_model_change_resizes_the_vector_to_the_new_symbol(self):
        """The donor's values belong to the old model; length must follow the new."""
        donor = _donor()
        preset = _preset([{"@model": "HD2_VerbStereo", "@position": 0, "Mix": 0.5}])
        overlay(donor, preset, _symbols())
        assert len(donor.values(1)) == 2  # not the donor's 3

    def test_stereo_spread_shifts_mix_and_the_overlay_follows(self):
        """Spread sits mid-list in the stereo variant, so Mix moves from 2 to 3."""
        symbols = _symbols()

        donor = _donor()
        overlay(
            donor,
            _preset([{"@model": "HD2_Chorus", "@position": 0, "@stereo": False, "Mix": 0.7}]),
            symbols,
        )
        assert donor.values(1)[2] == pytest.approx(0.7)

        donor = _donor()
        overlay(
            donor,
            _preset([{"@model": "HD2_Chorus", "@position": 0, "@stereo": True, "Mix": 0.7}]),
            symbols,
        )
        assert donor.values(1)[3] == pytest.approx(0.7)

    def test_a_parameter_the_symbol_lacks_is_reported_not_dropped_silently(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 0, "Nonexistent": 1.0}])
        report = overlay(donor, preset, _symbols())
        assert report.blocks[0].unmapped == ["Nonexistent"]
        assert not report.clean

    def test_unsupplied_parameters_are_reported(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 0, "Drive": 0.5}])
        report = overlay(donor, preset, _symbols())
        assert set(report.blocks[0].from_donor) == {"Bass", "Mid"}

    def test_structural_keys_are_never_treated_as_parameters(self):
        donor = _donor()
        preset = _preset(
            [{"@model": "HD2_AmpFoo", "@position": 0, "@type": 0, "@no_snapshot_bypass": False}]
        )
        report = overlay(donor, preset, _symbols())
        assert report.blocks[0].unmapped == []


class TestEmptySlots:
    def test_an_empty_slot_is_shaped_from_a_sibling_block(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_VerbStereo", "@position": 1, "Mix": 0.4}])
        report = overlay(donor, preset, _symbols())

        assert report.materialized == ["dsp0.block0"]
        assert not donor.is_empty_slot(2)
        assert donor.model_index(2) == 3

    def test_a_block_past_the_donors_slots_is_skipped_and_reported(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 99}])
        report = overlay(donor, preset, _symbols())
        assert report.skipped == ["dsp0.block0"]
        assert not report.clean


class TestEnabled:
    def test_enabled_flag_is_carried(self):
        donor = _donor()
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 0, "@enabled": False}])
        overlay(donor, preset, _symbols())
        assert donor._slot(0, 1)[KEY_CONTENT][KEY_ENABLED] is False


class TestDonorPreservation:
    def test_slots_the_tone_says_nothing_about_are_left_alone(self):
        """The whole point of a donor: untouched slots keep the device's values."""
        donor = _donor()
        before = [donor.values(i) for i in (11, 12)]
        overlay(donor, _preset([{"@model": "HD2_AmpFoo", "@position": 0}]), _symbols())
        assert [donor.values(i) for i in (11, 12)] == before
