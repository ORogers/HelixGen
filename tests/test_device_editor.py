"""Editor tests -- surgical block edits, against a fake session.

The rules under test are the ones the device enforces silently: it refuses a
value whose wire type is wrong rather than coercing it, and it takes an explicit
bypass state rather than a toggle.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from helixgen.device.editor import (
    K_ENABLED,
    K_MODEL_FLAG,
    K_MODEL_INDEX,
    K_MODEL_REF,
    K_PAIRED_INDEX,
    K_PARAM_INDEX,
    K_SLOT,
    K_VALUE,
    MODEL_MAIN,
    MODEL_PAIRED,
    NO_PAIRED,
    OP_BYPASS,
    OP_DELETE_BLOCK,
    OP_SAVE_PRESET,
    OP_SELECT_PRESET,
    OP_SET_VALUE,
    OP_SWAP_MODEL,
    BlockEditor,
    DspBudgetError,
    EditError,
    _wire_candidates,
    enum_index,
)
from helixgen.device.usb import DeviceRefusedError


class FakeSession:
    """Records commands; refuses whatever ``refuse`` says to."""

    def __init__(self, refuse=None):
        self.sent: list[tuple[int, dict]] = []
        self._refuse = refuse or (lambda op, target: None)

    def command(self, *, op: int, target: dict):
        self.sent.append((op, target))
        code = self._refuse(op, target)
        if code is not None:
            raise DeviceRefusedError(
                f"The device refused op {op} (code {code})", status=255, op=op, code=code
            )
        return None

    def ops(self) -> list[int]:
        return [op for op, _ in self.sent]


class TestSwapModel:
    def test_sends_the_model_ref_nested_in_the_target(self):
        session = FakeSession()
        BlockEditor(session).swap_model(3, 246)

        op, target = session.sent[0]
        assert op == OP_SWAP_MODEL
        assert target[K_SLOT] == 3
        assert target[K_MODEL_REF][K_MODEL_INDEX] == 246

    def test_unpaired_block_sets_the_flag_false_and_index_minus_one(self):
        session = FakeSession()
        BlockEditor(session).swap_model(1, 15)

        ref = session.sent[0][1][K_MODEL_REF]
        assert ref[K_MODEL_FLAG] is False
        assert ref[K_PAIRED_INDEX] == NO_PAIRED

    def test_a_paired_cab_sets_the_active_flag(self):
        """With the flag false the device stores the index but never instantiates it."""
        session = FakeSession()
        BlockEditor(session).swap_model(1, 15, paired=714)

        ref = session.sent[0][1][K_MODEL_REF]
        assert ref[K_MODEL_FLAG] is True
        assert ref[K_PAIRED_INDEX] == 714

    def test_dsp_budget_refusal_is_its_own_error(self):
        session = FakeSession(refuse=lambda op, _t: -306 if op == OP_SWAP_MODEL else None)
        with pytest.raises(DspBudgetError, match="DSP budget"):
            BlockEditor(session).swap_model(1, 15)

    def test_other_refusals_stay_generic(self):
        session = FakeSession(refuse=lambda op, _t: -3 if op == OP_SWAP_MODEL else None)
        with pytest.raises(EditError) as excinfo:
            BlockEditor(session).swap_model(1, 15)
        assert not isinstance(excinfo.value, DspBudgetError)


class TestWireTypes:
    def test_a_bool_is_only_ever_a_switch(self):
        assert _wire_candidates(True) == [True]

    def test_a_float_is_tried_as_float_first(self):
        assert _wire_candidates(0.25)[0] == pytest.approx(0.25)

    def test_an_integer_is_tried_as_a_float_first(self):
        """A mic distance reads as ``1`` and is continuous, not an enum."""
        candidates = _wire_candidates(45)
        assert candidates[0] == pytest.approx(45.0)
        assert isinstance(candidates[0], float)
        assert 45 in candidates

    def test_zero_and_one_may_also_be_switches(self):
        assert True in _wire_candidates(1)
        assert False in _wire_candidates(0)

    def test_a_string_has_no_wire_form(self):
        """An enum named by its label needs the catalog's value list."""
        assert _wire_candidates("2:1") == []


class TestEnumIndex:
    class _Definition:
        reverse_map: ClassVar[dict[str, int]] = {"2:1": 0, "4:1": 2}

    class _Model:
        def get_parameter(self, name):
            if name != "Ratio":
                raise KeyError(name)
            return TestEnumIndex._Definition()

    class _Catalog:
        def get(self, name):
            if name != "HD2_Comp":
                raise KeyError(name)
            return TestEnumIndex._Model()

    def test_a_label_resolves_to_its_index(self):
        """An .hlx names enum values the way the UI shows them."""
        assert enum_index(self._Catalog(), "HD2_Comp", "Ratio", "4:1") == 2

    def test_an_unknown_label_is_none_not_a_guess(self):
        assert enum_index(self._Catalog(), "HD2_Comp", "Ratio", "99:1") is None

    def test_an_unknown_model_or_parameter_is_none(self):
        assert enum_index(self._Catalog(), "HD2_Nope", "Ratio", "2:1") is None
        assert enum_index(self._Catalog(), "HD2_Comp", "Nope", "2:1") is None

    def test_no_catalog_means_no_resolution(self):
        assert enum_index(None, "HD2_Comp", "Ratio", "2:1") is None


class TestTrySetValue:
    def test_the_first_accepted_candidate_wins(self):
        session = FakeSession()
        assert BlockEditor(session).try_set_value(2, 5, 45)
        assert len(session.sent) == 1
        assert isinstance(session.sent[0][1][K_VALUE], float)

    def test_it_falls_back_when_the_device_refuses_the_first_type(self):
        def refuse(op, target):
            if op == OP_SET_VALUE and isinstance(target[K_VALUE], float):
                return -3
            return None

        session = FakeSession(refuse=refuse)
        assert BlockEditor(session).try_set_value(2, 5, 3)
        assert len(session.sent) == 2
        assert session.sent[1][1][K_VALUE] == 3

    def test_an_unrepresentable_value_reports_failure_without_sending(self):
        session = FakeSession()
        assert not BlockEditor(session).try_set_value(2, 5, "2:1")
        assert session.sent == []

    def test_every_candidate_refused_reports_failure(self):
        session = FakeSession(refuse=lambda op, _t: -3)
        assert not BlockEditor(session).try_set_value(2, 5, 1)


class TestSetValue:
    def test_targets_the_main_model_by_default(self):
        session = FakeSession()
        BlockEditor(session).set_value(4, 2, 0.5)
        target = session.sent[0][1]
        assert target[26] == MODEL_MAIN
        assert target[K_PARAM_INDEX] == 2

    def test_a_paired_cab_parameter_selects_the_paired_model(self):
        """Same bytes as a main edit but for key 26; dropping it moves the amp."""
        session = FakeSession()
        BlockEditor(session).set_value(4, 2, 0.5, model_sel=MODEL_PAIRED)
        assert session.sent[0][1][26] == MODEL_PAIRED


class TestEnabled:
    def test_it_is_an_explicit_state_not_a_toggle(self):
        session = FakeSession()
        editor = BlockEditor(session)
        editor.set_enabled(1, True)
        editor.set_enabled(1, False)

        assert [t[K_ENABLED] for _o, t in session.sent] == [True, False]
        assert session.ops() == [OP_BYPASS, OP_BYPASS]

    def test_key_59_carries_enabled_not_bypassed(self):
        """Measured on hardware: ``59: true`` turns the block **on**.

        Reading the op's name as "bypassed" inverts every block in the preset,
        and does it silently -- the chain is right, it is just all switched off.
        """
        session = FakeSession()
        BlockEditor(session).set_enabled(3, True)
        assert session.sent[0][1][K_ENABLED] is True


class TestSaveAndSelect:
    def test_save_carries_a_nul_terminated_name(self):
        session = FakeSession()
        BlockEditor(session).save_preset(0, 12, "Clean Tone")

        op, target = session.sent[0]
        assert op == OP_SAVE_PRESET
        assert target[109] == "Clean Tone\x00"

    def test_select_addresses_bank_and_preset(self):
        session = FakeSession()
        BlockEditor(session).select_preset(0, 42)
        assert session.sent[0][0] == OP_SELECT_PRESET
        assert session.sent[0][1][108] == 42

    def test_delete_block_is_slot_addressed(self):
        session = FakeSession()
        BlockEditor(session).delete_block(6)
        assert session.sent[0] == (OP_DELETE_BLOCK, {K_SLOT: 6})
