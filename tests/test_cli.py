import io
import json
from pathlib import Path
from typing import Any
from urllib import error as url_error

import pytest

from hlxgen import cli


def _write_chain(path: Path, chain: dict[str, object]) -> None:
    path.write_text(json.dumps(chain, indent=2), encoding="utf-8")


def _fake_ollama(
    chain: dict[str, object],
    parameters: dict[str, object] | None = None,
):
    """Build a urlopen stub that mimics Ollama's two-stage describe protocol.

    The first call returns the block list; hlxgen then makes one follow-up call
    per block asking for that block's parameters.
    """

    def fake_urlopen(request_obj: Any):
        prompt = json.loads(request_obj.data.decode("utf-8"))["prompt"]
        body: dict[str, object]
        if "Available parameters:" in prompt:
            body = {"parameters": parameters or {}}
        else:
            body = chain

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                payload = {"response": json.dumps(body), "done": True}
                return json.dumps(payload).encode("utf-8")

        return _Response()

    return fake_urlopen


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

    monkeypatch.setattr(
        "hlxgen.llm.request.urlopen",
        _fake_ollama(
            {"title": "Prompted Tone", "blocks": ["Horizon Drive"]},
            {"Drive": 0.6},
        ),
    )

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

    written = json.loads(preset_path.read_text(encoding="utf-8"))
    block0 = written["data"]["tone"]["dsp0"]["block0"]
    assert block0["@model"] == "HD2_DistHorizonDrive"
    assert block0["Drive"] == pytest.approx(0.6)


def test_cli_describe_upload_runs_script(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset_path = tmp_path / "upload-target.hlx"
    script_path = tmp_path / "import.applescript"
    script_path.write_text("-- dummy", encoding="utf-8")

    monkeypatch.setattr(
        "hlxgen.llm.request.urlopen",
        _fake_ollama({"title": "Upload Tone", "blocks": ["Horizon Drive"]}),
    )
    monkeypatch.setattr("hlxgen.cli.sys.platform", "darwin")

    captured_args: dict[str, Any] = {}

    def fake_upload(script: Path, preset: Path, mode: str) -> None:
        captured_args["script"] = script
        captured_args["preset"] = preset
        captured_args["mode"] = mode

    monkeypatch.setattr("hlxgen.cli._upload_via_script", fake_upload)

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
            "--upload",
            # Pinned: with a device attached the transport now defaults to USB,
            # and this test is specifically about the AppleScript fallback.
            "--upload-via",
            "applescript",
            "--upload-script",
            str(script_path),
        ]
    )

    assert exit_code == 0
    assert captured_args["script"] == script_path
    assert captured_args["preset"] == preset_path
    assert captured_args["mode"] == "auto"


def test_cli_describe_default_output_directory(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated_dir = tmp_path / "generated-presets"

    monkeypatch.setattr(
        "hlxgen.llm.request.urlopen",
        _fake_ollama({"title": "Prompted Tone", "blocks": ["Horizon Drive"]}),
    )
    monkeypatch.chdir(tmp_path)

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
            "--ollama-model",
            "fake-model",
        ]
    )

    assert exit_code == 0
    presets = list(generated_dir.glob("*.hlx"))
    assert len(presets) == 1


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


def test_cli_describe_reports_array_response(
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
                payload = {
                    "response": json.dumps(
                        [
                            {
                                "internal_name": "HD2_DistClean",
                                "params": {"Clean": 1.0},
                            }
                        ]
                    ),
                    "done": True,
                }
                return json.dumps(payload).encode("utf-8")

        return _Response()

    monkeypatch.setattr("hlxgen.llm.request.urlopen", fake_urlopen)

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
