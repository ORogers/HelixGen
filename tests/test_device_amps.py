"""Amp default-cab tests, and the chain planning that uses them.

An amp and its cab are one block on the pedal, not two. These tests pin the two
halves of that: reading each amp's own cab out of ``amp.models``, and folding a
tone's separate cab block into the amp's slot.
"""

from __future__ import annotations

import json

import pytest

from hlxgen.device.amps import AmpDataError, AmpDefaults
from hlxgen.device.editor import plan_chain
from hlxgen.device.symbols import DeviceSymbols

AMP_MODELS = json.dumps(
    [
        {
            "symbolicID": "HD2_AmpFoo",
            "name": "Foo Amp",
            "cablink": "HD2_CabFooPaired",
            "ircablink": "HD2_CabMicIr_FooIr",
        },
        {
            "symbolicID": "HD2_AmpBare",
            "name": "Bare Amp",
            "cablink": "",
            "ircablink": "",
        },
    ]
)


def _symbols() -> DeviceSymbols:
    return DeviceSymbols.parse(
        """[
          {"symbol": "HD2_DistFuzzMono", "parameters": ["Drive", "Level"]},
          {"symbol": "HD2_AmpFoo",       "parameters": ["Drive", "Bass", "Mid"]},
          {"symbol": "HD2_CabFooPaired", "parameters": ["Distance", "LowCut", "HighCut",
                                                        "EarlyReflections", "Level"]},
          {"symbol": "HD2_CabMicIr_FooIr", "parameters": ["Mic", "Position", "Distance",
                                                          "Angle", "LowCut", "HighCut",
                                                          "Level", "IrData"]},
          {"symbol": "HD2_VerbStereo",   "parameters": ["Decay", "Mix"]},
          {"symbol": "HD2_AmpBare",      "parameters": ["Drive"]}
        ]"""
    )


def _preset(blocks: list[dict]) -> dict:
    dsp = {f"block{i}": b for i, b in enumerate(blocks)}
    return {"data": {"tone": {"dsp0": dsp}}}


class TestAmpDefaults:
    def test_reads_both_cabs_per_amp(self):
        amps = AmpDefaults.parse(AMP_MODELS)
        amp = amps.get("HD2_AmpFoo")
        assert amp.paired_cab == "HD2_CabFooPaired"
        assert amp.ir_cab == "HD2_CabMicIr_FooIr"

    def test_identifies_amps(self):
        amps = AmpDefaults.parse(AMP_MODELS)
        assert amps.is_amp("HD2_AmpFoo")
        assert not amps.is_amp("HD2_DistFuzzMono")

    def test_an_empty_cablink_reads_as_none(self):
        amps = AmpDefaults.parse(AMP_MODELS)
        assert amps.paired_cab("HD2_AmpBare") is None

    def test_unknown_amp_has_no_cab(self):
        assert AmpDefaults.parse(AMP_MODELS).paired_cab("HD2_Nope") is None

    def test_bad_json_is_rejected(self):
        with pytest.raises(AmpDataError, match="not valid JSON"):
            AmpDefaults.parse("{not json")

    def test_a_non_array_is_rejected(self):
        with pytest.raises(AmpDataError, match="JSON array"):
            AmpDefaults.parse('{"symbolicID": "x"}')

    def test_an_empty_table_is_rejected(self):
        with pytest.raises(AmpDataError, match="no amp entries"):
            AmpDefaults.parse("[]")


class TestChainPlanning:
    def test_an_amp_carries_its_own_cab(self):
        preset = _preset([{"@model": "HD2_AmpFoo", "@position": 0}])
        planned, absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))

        assert len(planned) == 1
        assert planned[0]["paired_symbol"].symbol == "HD2_CabFooPaired"
        assert absorbed == []

    def test_a_following_cab_block_is_absorbed(self):
        """The tone describes in two slots what the pedal keeps in one."""
        preset = _preset(
            [
                {"@model": "HD2_AmpFoo", "@position": 0},
                {"@model": "HD2_CabMicIr_FooIr", "@position": 1},
                {"@model": "HD2_VerbStereo", "@position": 2},
            ]
        )
        planned, absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))

        assert [p["symbol"].symbol for p in planned] == ["HD2_AmpFoo", "HD2_VerbStereo"]
        assert len(absorbed) == 1
        assert planned[0]["cab_name"] == "dsp0.block1"

    def test_a_substituted_cab_is_reported(self):
        """The amp's own cab replaces the tone's; that is audible, so say it."""
        preset = _preset(
            [
                {"@model": "HD2_AmpFoo", "@position": 0},
                {"@model": "HD2_CabMicIr_FooIr", "@position": 1},
            ]
        )
        _planned, absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))
        assert "tone asked for HD2_CabMicIr_FooIr" in absorbed[0]

    def test_the_chain_closes_up_behind_an_absorbed_cab(self):
        """Fusing frees a slot; leaving a gap would waste it."""
        preset = _preset(
            [
                {"@model": "HD2_DistFuzzMono", "@position": 0},
                {"@model": "HD2_AmpFoo", "@position": 1},
                {"@model": "HD2_CabMicIr_FooIr", "@position": 2},
                {"@model": "HD2_VerbStereo", "@position": 3},
            ]
        )
        planned, _absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))
        assert [p["symbol"].symbol for p in planned] == [
            "HD2_DistFuzzMono",
            "HD2_AmpFoo",
            "HD2_VerbStereo",
        ]

    def test_a_cab_not_following_an_amp_keeps_its_slot(self):
        preset = _preset(
            [
                {"@model": "HD2_CabMicIr_FooIr", "@position": 0},
                {"@model": "HD2_VerbStereo", "@position": 1},
            ]
        )
        planned, absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))
        assert len(planned) == 2
        assert absorbed == []

    def test_an_amp_with_no_default_cab_is_left_alone(self):
        preset = _preset([{"@model": "HD2_AmpBare", "@position": 0}])
        planned, _absorbed = plan_chain(preset, _symbols(), AmpDefaults.parse(AMP_MODELS))
        assert planned[0]["paired_symbol"] is None

    def test_fusing_can_be_turned_off(self):
        preset = _preset(
            [
                {"@model": "HD2_AmpFoo", "@position": 0},
                {"@model": "HD2_CabMicIr_FooIr", "@position": 1},
            ]
        )
        planned, absorbed = plan_chain(
            preset, _symbols(), AmpDefaults.parse(AMP_MODELS), fuse_cabs=False
        )
        assert len(planned) == 2
        assert absorbed == []
        assert planned[0]["paired_symbol"] is None

    def test_without_amp_data_nothing_is_fused(self):
        """Absence is not fatal -- the cab simply keeps its own slot."""
        preset = _preset(
            [
                {"@model": "HD2_AmpFoo", "@position": 0},
                {"@model": "HD2_CabMicIr_FooIr", "@position": 1},
            ]
        )
        planned, absorbed = plan_chain(preset, _symbols(), None)
        assert len(planned) == 2
        assert absorbed == []
