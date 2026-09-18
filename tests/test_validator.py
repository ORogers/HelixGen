import copy

from hlxgen.dataset import ModelCatalog
from hlxgen.generator import generate_preset
from hlxgen.validator import PresetValidator


def test_validator_detects_invalid_parameter(dataset_path, schema_path, horizon_chain, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = copy.deepcopy(horizon_chain)
    preset, _ = generate_preset(chain, catalog, template_data)

    validator = PresetValidator(schema_path, catalog)
    structural_errors = validator.validate_structural(preset)
    assert structural_errors == []

    block = preset["data"]["tone"]["dsp0"]["block0"]
    block["Bogus"] = 0.5
    semantic_errors = validator.validate_semantic(preset)
    assert any("Bogus" in error for error in semantic_errors)


def test_validator_rejects_unknown_model(dataset_path, schema_path, horizon_chain, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = copy.deepcopy(horizon_chain)
    preset, _ = generate_preset(chain, catalog, template_data, overrides={"name": "Invalid"})
    preset["data"]["tone"]["dsp0"]["block0"]["@model"] = "HD2_InvalidModel"

    validator = PresetValidator(schema_path, catalog)
    errors = validator.validate_semantic(preset)
    assert any("not found in dataset" in error for error in errors)


def _preset_with_snapshots(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = {
        "meta": {"name": "Snaps"},
        "blocks": ["Horizon Drive"],
        "snapshots": [{"name": "Lead", "blocks": {"0": {"parameters": {"Drive": 0.7}}}}],
    }
    preset, _ = generate_preset(chain, catalog, template_data)
    return catalog, preset


def test_validator_accepts_generated_snapshots(dataset_path, schema_path, template_data):
    catalog, preset = _preset_with_snapshots(dataset_path, template_data)

    assert PresetValidator(schema_path, catalog).validate_semantic(preset) == []


def test_validator_rejects_dangling_snapshot_references(dataset_path, schema_path, template_data):
    catalog, preset = _preset_with_snapshots(dataset_path, template_data)
    tone = preset["data"]["tone"]
    tone["snapshot1"]["blocks"]["dsp0"]["block9"] = True
    tone["snapshot1"]["controllers"]["dsp0"]["block0"]["Level"] = {"@fs_enabled": False, "@value": 0.5}
    tone["controller"]["dsp0"]["block5"] = {"Drive": {"@controller": 9, "@min": 0.0, "@max": 1.0}}

    errors = PresetValidator(schema_path, catalog).validate_semantic(preset)

    assert "snapshot1.blocks.dsp0.block9: no such block in dsp0" in errors
    assert "snapshot1.controllers.dsp0.block0.Level: no matching controller assignment" in errors
    assert "controller.dsp0.block5: no such block in dsp0" in errors
