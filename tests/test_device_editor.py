"""Editor tests -- surgical block edits, against a fake session.

The rules under test are the ones the device enforces silently: it refuses a
value whose wire type is wrong rather than coercing it, and it takes an explicit
bypass state rather than a toggle.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from hlxgen.device.document import (
    CONTROLLER_SNAPSHOT,
    KEY_CONTENT,
    KEY_CONTROLLER_COUNT,
    KEY_CONTROLLER_GROUP,
    KEY_SLOT_ARRAY,
    KEY_SNAPSHOT_GROUP,
    KEY_TAG,
    KEY_VALUES,
    KEY_VECTOR,
    MAGIC,
    TAG_BLOCK,
    Document,
)
from hlxgen.device.editor import (
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
    ControllerTarget,
    DspBudgetError,
    EditError,
    EditReport,
    _apply_snapshot_controllers,
    _wire_candidates,
    enum_index,
)
from hlxgen.device.usb import DeviceRefusedError


class _Symbol:
    """A device symbol: the parameter order the wire addresses by index."""

    def __init__(self, ordinals: dict[str, int]):
        self._ordinals = ordinals

    def ordinal_of(self, name: str) -> int | None:
        return self._ordinals.get(name)


class _Catalog:
    """Just enough catalog to resolve an enum label to its index."""

    def __init__(self, maps: dict[str, dict[str, int]]):
        self._maps = maps

    def get(self, model_name: str):
        maps = self._maps

        class _Model:
            def get_parameter(self, name):
                class _Definition:
                    reverse_map = maps[name]

                return _Definition()

        return _Model()


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


class TestSnapshotControllers:
    """Writing a tone's snapshot-controlled parameters into the document.

    The device takes these from the document rather than from an edit op, and it
    refuses a value whose type is wrong -- so the types come from the block's own
    stored values, read back after the edits.
    """

    @staticmethod
    def _document(values: list | None = None, cab_values: list | None = None) -> Document:
        block = {
            KEY_TAG: TAG_BLOCK,
            KEY_CONTENT: {
                KEY_VALUES: {2: 0, 3: 0, KEY_VECTOR: list(values or [0.4, 0.5, True])},
                12: {2: 0, 3: 0, KEY_VECTOR: list(cab_values or [])},
            },
        }
        preset = {
            0: {KEY_SLOT_ARRAY: [None, block, None]},
            KEY_CONTROLLER_GROUP: [None] * 10,
            KEY_SNAPSHOT_GROUP: {
                6: 0,
                8: 0,
                10: [
                    {2: [[False, 64, None] for _ in range(64)], 4: b"S\x00"}
                    for _ in range(3)
                ],
            },
        }
        return Document(magic=MAGIC, header_slots=[], header_len=0, preset=preset)

    @staticmethod
    def _tone(*, values: list, minimum=0.0, maximum=1.0, controller: int = 9) -> dict:
        tone: dict = {
            "controller": {
                "dsp0": {"block0": {"Drive": {"@controller": controller, "@min": minimum, "@max": maximum}}}
            }
        }
        for index, value in enumerate(values):
            tone[f"snapshot{index}"] = {
                "controllers": {"dsp0": {"block0": {"Drive": {"@fs_enabled": False, "@value": value}}}}
            }
        return tone

    @staticmethod
    def _targets(symbol=None, *, model_sel: int = MODEL_MAIN, block: dict | None = None) -> dict:
        return {
            "dsp0.block0": ControllerTarget(
                1, symbol or _Symbol({"Drive": 0}), model_sel, block or {"@model": "Brit Plexi Brt"}
            )
        }

    def _apply(self, document, tone, targets=None, catalog=None) -> EditReport:
        report = EditReport()
        if targets is None:
            targets = self._targets()
        _apply_snapshot_controllers(document, tone, targets, catalog, report)
        return report

    def test_writes_an_assignment_and_a_value_per_snapshot(self):
        document = self._document()
        report = self._apply(document, self._tone(values=[0.2, 0.45, 0.6]))

        assert report.snapshot_controls == ["dsp0.block0.Drive"]
        (assignment,) = document.controller_assignments(CONTROLLER_SNAPSHOT)
        assert assignment[1][5] == 1  # the block's slot
        assert assignment[1][6][29] == 0  # the parameter's device ordinal
        assert [s[2][0][2] for s in document.snapshots()] == [0.2, 0.45, 0.6]
        assert document.preset[KEY_SNAPSHOT_GROUP][KEY_CONTROLLER_COUNT] == 1

    def test_a_snapshot_that_does_not_change_it_keeps_the_block_value(self):
        document = self._document(values=[0.4])
        tone = self._tone(values=[0.2, 0.6])
        del tone["snapshot1"]
        tone["snapshot2"] = {}

        self._apply(document, tone)

        assert [s[2][0][2] for s in document.snapshots()] == [0.2, 0.4, 0.4]

    def test_values_take_the_type_the_device_stores(self):
        """A switch written as 1 must go as a bool: the device refuses an int."""
        document = self._document(values=[0.4, 0.5, True])
        tone = self._tone(values=[1, 0, True], minimum=False, maximum=True)
        tone["controller"]["dsp0"]["block0"] = {
            "Bright": {"@controller": 9, "@min": False, "@max": True}
        }
        for index in range(3):
            tone[f"snapshot{index}"]["controllers"]["dsp0"]["block0"] = {
                "Bright": {"@value": [1, 0, True][index]}
            }

        self._apply(document, tone, self._targets(_Symbol({"Bright": 2})))

        assert [s[2][0][2] for s in document.snapshots()] == [True, False, True]
        assert document.controller_assignments(CONTROLLER_SNAPSHOT)[0][1][2] is False

    def test_an_enum_label_resolves_through_the_catalog(self):
        document = self._document(values=[0, 1, 2])
        tone = self._tone(values=["4:1", "2:1", "2:1"])

        report = self._apply(document, tone, catalog=_Catalog({"Drive": {"2:1": 0, "4:1": 2}}))

        assert report.snapshot_controls == ["dsp0.block0.Drive"]
        assert [s[2][0][2] for s in document.snapshots()] == [2, 0, 0]

    def test_a_fused_cab_is_addressed_as_the_amp_s_paired_model(self):
        """A cab inside its amp is the amp's slot plus the paired sub-model.

        Its values live in the block's second vector, so the type comes from
        there too -- reading the amp's would give the wrong parameter.
        """
        document = self._document(values=[0.4], cab_values=[0.1, 0.9])
        targets = self._targets(_Symbol({"Drive": 1}), model_sel=MODEL_PAIRED)

        self._apply(document, self._tone(values=[0.3, 0.3, 0.3]), targets)

        (assignment,) = document.controller_assignments(CONTROLLER_SNAPSHOT)
        assert assignment[1][7] == MODEL_PAIRED
        assert assignment[1][6][28] == MODEL_PAIRED
        assert assignment[1][6][29] == 1

    def test_the_donor_s_assignments_are_dropped(self):
        """An assignment left behind would drive whatever block now holds its slot."""
        document = self._document()
        document.preset[KEY_CONTROLLER_GROUP][1] = [{0: 0, 1: {0: 1, 5: 7}}]
        for snapshot in document.snapshots():
            snapshot[2][0] = [False, 0, 0.9]

        self._apply(document, self._tone(values=[0.2, 0.2, 0.2]))

        assert document.preset[KEY_CONTROLLER_GROUP][1] is None
        assert document.preset[KEY_SNAPSHOT_GROUP][KEY_CONTROLLER_COUNT] == 1

    @pytest.mark.parametrize(
        ("kwargs", "symbol"),
        [
            ({"controller": 1}, None),  # an expression pedal, which push cannot write
            ({}, _Symbol({})),  # a parameter the device model has no ordinal for
        ],
    )
    def test_what_cannot_be_written_is_reported(self, kwargs, symbol):
        document = self._document()
        report = self._apply(
            document, self._tone(values=[0.2, 0.2, 0.2], **kwargs), self._targets(symbol)
        )

        assert report.unsupported_controllers == ["dsp0.block0.Drive"]
        assert report.snapshot_controls == []
        assert document.controller_assignments(CONTROLLER_SNAPSHOT) == []

    def test_a_block_that_never_landed_is_reported(self):
        report = self._apply(self._document(), self._tone(values=[0.2, 0.2, 0.2]), {})
        assert report.unsupported_controllers == ["dsp0.block0.Drive"]
