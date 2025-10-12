from __future__ import annotations

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
