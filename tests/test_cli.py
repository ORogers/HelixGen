from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hlxgen import cli
from hlxgen.llm import LLMGenerationError


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


def test_cli_generate_accepts_string_blocks(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
) -> None:
    chain_path = tmp_path / "simple.json"
    preset_path = tmp_path / "simple.hlx"
    chain: dict[str, object] = {
        "title": "Minimal Chain",
        "author": "Tone Bot",
        "description": "Defaults only",
        "blocks": [
            "Horizon Drive",
            "Transistor Tape",
        ],
    }
    _write_chain(chain_path, chain)

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

    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    meta = preset["data"]["meta"]
    assert meta["name"] == "Minimal Chain"
    assert meta["author"] == "Tone Bot"
    assert meta["description"] == "Defaults only"

    block0 = preset["data"]["tone"]["dsp0"]["block0"]
    assert block0["@model"] == "HD2_DistHorizonDrive"
    assert "Drive" in block0


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


def test_cli_describe_generates_from_prompt(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset_path = tmp_path / "prompted.hlx"

    def fake_invoke(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        return {"title": "Prompted Tone", "blocks": ["Horizon Drive"]}

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_invoke)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "Warm fuzzy lead sound",
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
            "--output",
            str(preset_path),
            "--ollama-model",
            "fake-model",
        ]
    )

    assert exit_code == 0
    assert preset_path.exists()


def test_cli_describe_reports_invalid_llm_json(
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def fake_invoke(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        raise LLMGenerationError("LLM response did not contain valid JSON")

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_invoke)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "Give me something impossible",
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "LLM error" in captured.err


def test_cli_describe_reports_array_response(
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def fake_invoke(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        raise LLMGenerationError("LLM response must be a JSON object that matches the chain schema")

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_invoke)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "clean tone",
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "JSON object" in captured.err


def test_cli_describe_reports_http_error(
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def fake_invoke(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        raise LLMGenerationError(
            "Ollama returned HTTP 404 (Not Found). Ensure the endpoint URL includes the /api/generate path or adjust it via --ollama-endpoint."
        )

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_invoke)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "Play me a crunchy rhythm",
            "--schema",
            str(schema_path),
            "--template",
            str(template_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Ollama returned HTTP 404" in captured.err
    assert "/api/generate" in captured.err
