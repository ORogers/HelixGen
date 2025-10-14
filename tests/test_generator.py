from __future__ import annotations

import copy

from hlxgen.dataset import ModelCatalog
from hlxgen.generator import generate_preset


def test_generate_preset_applies_defaults_and_overrides(dataset_path, horizon_chain, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = copy.deepcopy(horizon_chain)

    preset, report = generate_preset(
        chain,
        catalog,
        template_data,
        overrides={"name": "Stage Ready", "tempo": 140.0, "author": "Test", "device": "Helix Native"},
    )

    data = preset["data"]
    meta = data["meta"]
    tone = data["tone"]
    block = tone["dsp0"]["block0"]

    assert data["device"] == 2162694
    assert data["device_version"] == 57671680
    assert meta["appversion"] == 58851328
    assert meta["name"] == "Stage Ready"
    assert meta["author"] == "Test"
    assert meta["device"] == "Helix Native"
    assert tone["global"]["@tempo"] == 140.0
    assert block["@model"] == "HD2_DistHorizonDrive"
    assert block["@enabled"] is False
    assert block["Bright"] == 0.2

    # Dataset default should be applied for parameters not supplied in chain
    assert "Gate" in block
    assert "Level" in block

    # Drive input of 2.0 should clamp to dataset max (1.0) and produce a warning
    assert block["Drive"] == 1.0
    assert any("clamped to maximum" in warning for warning in report.warnings)

    footswitch = tone["footswitch"]["dsp0"]["block0"]
    assert footswitch["@fs_index"] == 1
    assert footswitch["@fs_enabled"] is True


def test_non_distortion_blocks_remain_enabled(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = {"blocks": [{"model": "US Double Nrm"}]}

    preset, _ = generate_preset(chain, catalog, template_data)
    block = preset["data"]["tone"]["dsp0"]["block0"]

    assert block["@model"] == "HD2_AmpUSDoubleNrm"
    assert block["@enabled"] is True
