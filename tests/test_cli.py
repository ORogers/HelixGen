from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib import error as url_error

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


def test_cli_describe_generates_from_prompt(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset_path = tmp_path / "prompted.hlx"

    def fake_urlopen(request_obj: Any):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                chain = {
                    "meta": {"name": "Prompted Tone"},
                    "global": {"@tempo": 100.0},
                    "input": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 1},
                    "output": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1},
                    "blocks": [
                        {
                            "model": "Horizon Drive",
                            "parameters": {"Drive": 2.5},
                        }
                    ],
                }
                payload = {"response": json.dumps(chain), "done": True}
                return json.dumps(payload).encode("utf-8")

        return _Response()

    monkeypatch.setattr("hlxgen.llm.request.urlopen", fake_urlopen)

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
    def fake_urlopen(_: Any):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                payload = {"response": "not-json", "done": True}
                return json.dumps(payload).encode("utf-8")

        return _Response()

    monkeypatch.setattr("hlxgen.llm.request.urlopen", fake_urlopen)

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


def test_cli_describe_reports_http_error(
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def fake_urlopen(_: Any):
        raise url_error.HTTPError(
            url="http://localhost:11434/api/generate",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr("hlxgen.llm.request.urlopen", fake_urlopen)

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
