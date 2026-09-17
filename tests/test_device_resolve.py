import json

import pytest

from hlxgen.dataset import ModelCatalog
from hlxgen.device.resolve import (
    ResolutionError,
    audit_catalog,
    host_parameter_names,
    resolve_model,
)
from hlxgen.device.symbols import DeviceSymbols

# A miniature catalog in the shape of helix_model_information.json, paired with a
# symbol table that exercises the three cases that matter: an unsuffixed model, one
# the device splits into Mono/Stereo, and one the device does not carry at all.
CATALOG = {
    "Horizon Drive": {
        "internal_model_name": "HD2_DistHorizonDrive",
        "category": "Distortion",
        "parameters": {
            "Drive": {"valueType": 1, "min": 0.0, "max": 1.0, "default": 0.5},
            "Level": {"valueType": 1, "min": 0.0, "max": 1.0, "default": 0.5},
            "Bright": {"valueType": 1, "min": 0.0, "max": 1.0, "default": 0.5},
            "@enabled": {"valueType": 2, "displayType": "boolean"},
        },
    },
    "70s Chorus": {
        "internal_model_name": "HD2_Chorus70sChorus",
        "category": "Modulation",
        "parameters": {
            "Mode": {"valueType": 1, "min": 0.0, "max": 1.0},
            "Spread": {"valueType": 1, "min": 0.0, "max": 1.0},
            "Mix": {"valueType": 1, "min": 0.0, "max": 1.0},
            "Level": {"valueType": 1, "min": 0.0, "max": 1.0},
        },
    },
    "Phantom Pedal": {
        "internal_model_name": "HD2_NotOnThisDevice",
        "category": "Distortion",
        "parameters": {"Gain": {"valueType": 1, "min": 0.0, "max": 1.0}},
    },
}

SYMBOLS = [
    {"symbol": "HD2_DistHorizonDrive", "parameters": ["Drive", "Level", "Bright"]},
    {"symbol": "HD2_Chorus70sChorusMono", "parameters": ["Mode", "Mix", "Level"]},
    {"symbol": "HD2_Chorus70sChorusStereo", "parameters": ["Mode", "Spread", "Mix", "Level"]},
]


@pytest.fixture
def catalog(tmp_path) -> ModelCatalog:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(CATALOG), encoding="utf-8")
    return ModelCatalog(path)


@pytest.fixture
def symbols() -> DeviceSymbols:
    return DeviceSymbols.parse(json.dumps(SYMBOLS))


def test_host_parameter_names_drops_the_at_prefixed_pseudo_parameters(catalog):
    names = host_parameter_names(catalog.get("Horizon Drive"))
    assert names == ["Drive", "Level", "Bright"]


def test_resolve_model_yields_the_wire_index_and_variant(catalog, symbols):
    resolved = resolve_model(catalog.get("Horizon Drive"), symbols)
    assert resolved.index == 0
    assert resolved.variant == ""

    stereo = resolve_model(catalog.get("70s Chorus"), symbols, stereo=True)
    assert stereo.symbol.symbol == "HD2_Chorus70sChorusStereo"
    assert stereo.index == 2
    assert stereo.variant == "Stereo"


def test_resolve_model_raises_for_a_model_the_device_lacks(catalog, symbols):
    with pytest.raises(ResolutionError, match="HD2_NotOnThisDevice"):
        resolve_model(catalog.get("Phantom Pedal"), symbols)


def test_value_vector_follows_device_order_not_catalog_order(catalog, symbols):
    values = {"Level": 0.8, "Mix": 0.25, "Mode": 0.1, "Spread": 0.9}

    mono = resolve_model(catalog.get("70s Chorus"), symbols, stereo=False)
    assert mono.device_parameters == ("Mode", "Mix", "Level")
    assert mono.value_vector(values) == [0.1, 0.25, 0.8]

    stereo = resolve_model(catalog.get("70s Chorus"), symbols, stereo=True)
    assert stereo.value_vector(values) == [0.1, 0.9, 0.25, 0.8]


def test_value_vector_marks_unsupplied_parameters_rather_than_shifting(catalog, symbols):
    resolved = resolve_model(catalog.get("Horizon Drive"), symbols)
    assert resolved.value_vector({"Drive": 0.7, "Bright": 0.2}) == [0.7, None, 0.2]


def test_ordinals_shift_between_variants(catalog, symbols):
    mono = resolve_model(catalog.get("70s Chorus"), symbols, stereo=False)
    stereo = resolve_model(catalog.get("70s Chorus"), symbols, stereo=True)
    assert mono.ordinal_of("Mix") == 1
    assert stereo.ordinal_of("Mix") == 2
    assert mono.ordinal_of("Spread") is None


def test_audit_reports_coverage_and_the_variant_split(catalog, symbols):
    report = audit_catalog(catalog, symbols)
    assert report.symbol_count == 3
    assert report.catalog_count == 3
    assert len(report.resolved) == 2
    assert [m.display_name for m in report.unresolved] == ["Phantom Pedal"]
    assert [m.display_name for m in report.split_models] == ["70s Chorus"]


def test_audit_flags_parameters_that_do_not_reconcile(catalog, symbols):
    report = audit_catalog(catalog, symbols)
    chorus = next(m for m in report.models if m.display_name == "70s Chorus")

    # Mono has no Spread, so the catalog carries one parameter the device does not.
    assert chorus.missing_on_device["Mono"] == ["Spread"]
    assert chorus.missing_on_device["Stereo"] == []
    assert chorus.device_parameter_counts == {"Mono": 3, "Stereo": 4}
    assert not chorus.clean

    horizon = next(m for m in report.models if m.display_name == "Horizon Drive")
    assert horizon.clean
    assert not horizon.split_by_variant


def test_audit_is_json_serialisable(catalog, symbols):
    payload = audit_catalog(catalog, symbols).to_dict()
    assert json.loads(json.dumps(payload))["unresolved"] == 1
    assert payload["unresolved_models"][0]["internal"] == "HD2_NotOnThisDevice"
