"""Discrete ("list") parameters must be written at the value the device uses.

A model's option list is enumerated from zero in the catalog, but the number a
preset stores is the device's own, and several option lists do not start at
zero: a delay's note sync runs 1..19, an IR select 1..128, a harmoniser's
interval -8..8. Writing the list position instead of the device value lands the
block on a neighbouring option -- a dotted eighth delay arrives as a quarter --
and does it silently, because the wrong number is a perfectly valid option.
"""

from __future__ import annotations

import copy

import pytest

from helixgen.dataset import DISCRETE, ModelCatalog
from helixgen.generator import generate_preset

#: Device value -> label, from HX Edit's own ``sync_note`` list, which the
#: firmware stores one-based.
SYNC_NOTE = {
    1: "1/1",
    2: "1/2 Dotted",
    3: "1/2",
    4: "1/2 Triplet",
    5: "1/4 Dotted",
    6: "1/4",
    7: "1/4 Triplet",
    8: "1/8 Dotted",
    9: "1/8",
    10: "1/8 Triplet",
    11: "1/16 Dotted",
    12: "1/16",
    13: "1/16 Triplet",
    14: "1/32 Dotted",
    15: "1/32",
    16: "1/32 Triplet",
    17: "1/64 Dotted",
    18: "1/64",
    19: "1/64 Triplet",
}


@pytest.fixture
def catalog(dataset_path) -> ModelCatalog:
    return ModelCatalog(dataset_path)


@pytest.mark.parametrize(("label", "expected"), sorted((v, k) for k, v in SYNC_NOTE.items()))
def test_note_sync_normalizes_to_the_device_value(catalog, label, expected):
    definition = catalog.get("Transistor Tape").get_parameter("SyncSelect1")
    assert definition.normalize(label) == expected
    assert definition.forward_map[str(expected)] == label


def test_note_sync_default_is_a_quarter_note(catalog):
    """The firmware's own default, and the one a tone that says nothing gets."""
    definition = catalog.get("Transistor Tape").get_parameter("SyncSelect1")
    assert definition.default_value() == 6


def test_dotted_eighth_delay_is_written_as_a_dotted_eighth(catalog, template_data):
    """The end-to-end shape of the bug: the tone asks for a dotted eighth and
    the preset that reaches HX Edit has to hold the dotted eighth's number."""
    chain = {
        "title": "Dotted Eighth",
        "blocks": [
            {
                "model": "Transistor Tape",
                "parameters": {"TempoSync1": "On", "SyncSelect1": "1/8 Dotted"},
            }
        ],
    }
    preset, _ = generate_preset(copy.deepcopy(chain), catalog, template_data)
    block = preset["data"]["tone"]["dsp0"]["block0"]
    assert SYNC_NOTE[block["SyncSelect1"]] == "1/8 Dotted"


@pytest.mark.parametrize(
    ("model", "parameter", "label", "expected"),
    [
        # Option lists that start somewhere other than zero.
        ("Twin Harmony", "IntervalVoice1", "-9th", -8),
        ("Twin Harmony", "IntervalVoice1", "0", 0),
        ("Twin Harmony", "IntervalVoice1", "9th", 8),
        ("Poly Pitch", "ShiftCurve", "Linear", 0),
        ("Crisscross", "Shape", "Sine", 2),
        ("Crisscross", "Shape", "Triangle", 3),
        # ...and one that does, which must stay put.
        ("4x12 Greenback 25", "Mic", "57 Dynamic", 0),
    ],
)
def test_offset_option_lists_normalize_to_the_device_value(
    catalog, model, parameter, label, expected
):
    definition = catalog.get(model).get_parameter(parameter)
    assert definition.normalize(label) == expected


def test_every_option_list_runs_from_min_to_max(catalog):
    """The catalog's own invariant, which is what went wrong here.

    A discrete parameter's option numbers are the device's, so they have to run
    contiguously from the parameter's ``min`` to its ``max``. An option list
    enumerated from zero regardless of ``min`` is exactly the defect that sent
    dotted eighths to the pedal as quarters, and it is invisible in the
    generated preset -- the number it writes is a valid option, just not the
    one the tone asked for.
    """
    offenders = []
    for model in catalog.models():
        for name in model.parameter_names():
            definition = model.get_parameter(name)
            if definition.value_type != DISCRETE or not definition.forward_map:
                continue
            keys = sorted(int(k) for k in definition.forward_map)
            expected = list(range(int(definition.min_value), int(definition.max_value) + 1))
            if keys != expected:
                offenders.append(f"{model.display_name}.{name}: {keys[:3]}... != {expected[:3]}...")
    assert not offenders, "\n".join(offenders)


def test_every_discrete_parameter_declares_its_range(catalog):
    missing = [
        f"{model.display_name}.{name}"
        for model in catalog.models()
        for name in model.parameter_names()
        if (definition := model.get_parameter(name)).value_type == DISCRETE
        and (definition.min_value is None or definition.max_value is None)
    ]
    assert not missing
