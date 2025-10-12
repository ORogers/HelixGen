from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlxgen import cli


def _write_chain(path: Path, chain: dict[str, object]) -> None:
    path.write_text(json.dumps(chain, indent=2), encoding="utf-8")


def test_cli_generate_writes_preset(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    horizon_chain: dict[str, object],
    template_path: Path,
) -> None:
    chain_path = tmp_path / "chain.json"
    preset_path = tmp_path / "preset.hlx"
    _write_chain(chain_path, horizon_chain)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "generate",
            str(chain_path),
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
            "--output",
            str(preset_path),
        ]
    )

    assert exit_code == 0
    assert preset_path.exists()


def test_cli_validate_creates_report(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    horizon_chain: dict[str, object],
    template_path: Path,
) -> None:
    chain_path = tmp_path / "chain.json"
    preset_path = tmp_path / "preset.hlx"
    report_path = tmp_path / "report.json"
    _write_chain(chain_path, horizon_chain)

    generate_exit = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "generate",
            str(chain_path),
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
            "--output",
            str(preset_path),
        ]
    )
    assert generate_exit == 0

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "validate",
            str(preset_path),
            "--schema",
            str(schema_path),
            "--report",
            str(report_path),
        ]
    )
    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["valid"] is True


def test_cli_inspect_outputs_table(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    horizon_chain: dict[str, object],
    template_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    chain_path = tmp_path / "chain.json"
    preset_path = tmp_path / "preset.hlx"
    _write_chain(chain_path, horizon_chain)

    generate_exit = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "generate",
            str(chain_path),
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
            "--output",
            str(preset_path),
        ]
    )
    assert generate_exit == 0

    exit_code = cli.main(
        [
            "inspect",
            str(preset_path),
            "--dataset",
            str(dataset_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Model (ID)" in captured.out
    assert "HD2_DistHorizonDrive" in captured.out


def test_cli_models_lists_dataset(dataset_path: Path, capsys: pytest.CaptureFixture) -> None:
    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "models",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Model" in captured.out
    assert "Horizon Drive" in captured.out
    assert "HD2_DistHorizonDrive" in captured.out
