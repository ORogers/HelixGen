import copy

import pytest

from helixgen.dataset import ModelCatalog, ModelCatalogError
from helixgen.generator import SNAPSHOT_CONTROLLER, generate_preset


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
    assert block["@enabled"] is True
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


def _snapshot_chain(snapshots):
    return {
        "meta": {"name": "Snapshots"},
        "blocks": [
            "Scream 808",
            {"model": "Brit Plexi Brt", "parameters": {"Drive": 0.45}},
            "4x12 Greenback 25",
            {"model": "Simple Delay", "enabled": False},
        ],
        "snapshots": snapshots,
    }


def test_generate_preset_writes_snapshots(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = _snapshot_chain(
        [
            {"name": "Clean", "blocks": {"0": {"enabled": False}, "1": {"parameters": {"Drive": 0.2}}}},
            {"name": "Rhythm"},
            {"name": "Solo", "blocks": {"3": {"enabled": True, "parameters": {"Mix": 0.4}}}},
        ]
    )

    preset, report = generate_preset(chain, catalog, template_data)
    tone = preset["data"]["tone"]

    assert report.warnings == []
    assert [tone[f"snapshot{n}"]["@name"] for n in range(3)] == ["Clean", "Rhythm", "Solo"]
    assert all(tone[f"snapshot{n}"]["@custom_name"] is True for n in range(3))

    # Bypass states: unchanged blocks keep their own @enabled.
    assert tone["snapshot0"]["blocks"]["dsp0"] == {
        "block0": False, "block1": True, "block2": True, "block3": False,
    }
    assert tone["snapshot1"]["blocks"]["dsp0"] == {
        "block0": True, "block1": True, "block2": True, "block3": False,
    }
    assert tone["snapshot2"]["blocks"]["dsp0"]["block3"] is True

    # Every parameter a snapshot touches gets a snapshot controller over its full range.
    assert tone["controller"]["dsp0"] == {
        "block1": {"Drive": {"@controller": SNAPSHOT_CONTROLLER, "@max": 1.0, "@min": 0.0}},
        "block3": {"Mix": {"@controller": SNAPSHOT_CONTROLLER, "@max": 1.0, "@min": 0.0}},
    }
    drive = [tone[f"snapshot{n}"]["controllers"]["dsp0"]["block1"]["Drive"] for n in range(3)]
    assert drive == [
        {"@fs_enabled": False, "@value": 0.2},
        {"@fs_enabled": False, "@value": 0.45},
        {"@fs_enabled": False, "@value": 0.45},
    ]
    mix = [tone[f"snapshot{n}"]["controllers"]["dsp0"]["block3"]["Mix"]["@value"] for n in range(3)]
    assert mix[2] == 0.4
    assert mix[0] == mix[1] == catalog.get("Simple Delay").parameters["Mix"].default_value()

    # dsp0 mirrors the snapshot the preset loads on.
    assert tone["global"]["@current_snapshot"] == 0
    assert tone["dsp0"]["block0"]["@enabled"] is False
    assert tone["dsp0"]["block1"]["Drive"] == 0.2


def test_generate_preset_without_snapshots_mirrors_block_state(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = _snapshot_chain(None)
    del chain["snapshots"]

    preset, _ = generate_preset(chain, catalog, template_data)
    tone = preset["data"]["tone"]

    assert "controller" not in tone
    for n in range(3):
        assert tone[f"snapshot{n}"]["blocks"]["dsp0"]["block3"] is False
        assert tone[f"snapshot{n}"]["@name"] == f"SNAPSHOT {n + 1}"


def test_generate_preset_truncates_long_snapshot_names(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = _snapshot_chain([{"name": "Ambient Wash"}])

    preset, report = generate_preset(chain, catalog, template_data)

    assert preset["data"]["tone"]["snapshot0"]["@name"] == "Ambient Wa"
    assert any("truncated" in warning for warning in report.warnings)


@pytest.mark.parametrize(
    ("snapshots", "message"),
    [
        ([{}, {}, {}, {}], "only provides 3"),
        ([{"blocks": {"7": {"enabled": False}}}], "chain has 4 blocks"),
        ([{"blocks": {"delay": {"enabled": False}}}], "zero-based index"),
        ([{"blocks": {"1": {"parameters": {"Bogus": 1}}}}], "not valid"),
        ([{"blocks": {"1": {"enabled": "yes"}}}], "true or false"),
        ("Clean", "must be a list"),
    ],
)
def test_generate_preset_rejects_bad_snapshots(dataset_path, template_data, snapshots, message):
    catalog = ModelCatalog(dataset_path)

    with pytest.raises(ModelCatalogError, match=message):
        generate_preset(_snapshot_chain(snapshots), catalog, template_data)


def test_generate_preset_rejects_snapshot_bypass_on_pinned_block(dataset_path, template_data):
    catalog = ModelCatalog(dataset_path)
    chain = _snapshot_chain([{"blocks": {"0": {"enabled": False}}}])
    chain["blocks"][0] = {"model": "Scream 808", "no_snapshot_bypass": True}

    with pytest.raises(ModelCatalogError, match="no_snapshot_bypass"):
        generate_preset(chain, catalog, template_data)
