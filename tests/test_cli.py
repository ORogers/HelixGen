import io
import json
from pathlib import Path
from typing import Any
from urllib import error as url_error

import pytest

from helixgen import cli


def _write_chain(path: Path, chain: dict[str, object]) -> None:
    path.write_text(json.dumps(chain, indent=2), encoding="utf-8")


_DEFAULT_SNAPSHOTS = [{"name": f"Snap {n}", "blocks": {}} for n in (1, 2, 3)]


def _fake_ollama(
    chain: dict[str, object],
    parameters: dict[str, object] | None = None,
    snapshots: list[dict[str, object]] | None = None,
):
    """Build a urlopen stub that mimics Ollama's three-round describe protocol.

    The first call returns the block list; the second sets the parameters of
    every block at once, and ``parameters`` is what the first block receives;
    the third designs the preset's snapshots. Each request payload is recorded
    on the stub's ``payloads`` list.
    """

    payloads: list[dict[str, object]] = []

    def fake_urlopen(request_obj: Any, timeout: float | None = None):
        payload = json.loads(request_obj.data.decode("utf-8"))
        payloads.append(payload)
        body: dict[str, object]
        if "Blocks to configure:" in payload["prompt"]:
            body = {"blocks": {"block1": parameters or {}}}
        elif "Blocks, in signal order:" in payload["prompt"]:
            body = {"snapshots": snapshots or _DEFAULT_SNAPSHOTS}
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

    fake_urlopen.payloads = payloads
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
        "helixgen.llm.request.urlopen",
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
            "--llm-backend",
            "ollama",
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
        "helixgen.llm.request.urlopen",
        _fake_ollama({"title": "Upload Tone", "blocks": ["Horizon Drive"]}),
    )
    monkeypatch.setattr("helixgen.cli.sys.platform", "darwin")

    captured_args: dict[str, Any] = {}

    def fake_upload(script: Path, preset: Path, mode: str) -> None:
        captured_args["script"] = script
        captured_args["preset"] = preset
        captured_args["mode"] = mode

    monkeypatch.setattr("helixgen.cli._upload_via_script", fake_upload)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "--llm-backend",
            "ollama",
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
        "helixgen.llm.request.urlopen",
        _fake_ollama({"title": "Prompted Tone", "blocks": ["Horizon Drive"]}),
    )
    monkeypatch.chdir(tmp_path)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "--llm-backend",
            "ollama",
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
    def fake_urlopen(_: Any, timeout: float | None = None):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                payload = {"response": "not-json", "done": True}
                return json.dumps(payload).encode("utf-8")

        return _Response()

    monkeypatch.setattr("helixgen.llm.request.urlopen", fake_urlopen)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "--llm-backend",
            "ollama",
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
    def fake_urlopen(_: Any, timeout: float | None = None):
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

    monkeypatch.setattr("helixgen.llm.request.urlopen", fake_urlopen)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "--llm-backend",
            "ollama",
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
    def fake_urlopen(_: Any, timeout: float | None = None):
        raise url_error.HTTPError(
            url="http://localhost:11434/api/generate",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr("helixgen.llm.request.urlopen", fake_urlopen)

    exit_code = cli.main(
        [
            "--dataset",
            str(dataset_path),
            "describe",
            "--llm-backend",
            "ollama",
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


def test_hx_edit_flag_rejects_a_path_that_is_not_an_install(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Told at the front door, not several layers down in a USB write."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--hx-edit", str(tmp_path), "models"])

    assert excinfo.value.code == 2
    assert "does not look like an HX Edit install" in capsys.readouterr().err


def test_hx_edit_flag_points_the_discovery_at_a_chosen_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helixgen.device import hxedit

    resources = tmp_path / "HX Edit.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "Helix.sym").write_text("[]", encoding="utf-8")
    monkeypatch.delenv(hxedit.HX_EDIT_ENV_VAR, raising=False)

    assert cli.main(["--hx-edit", str(tmp_path / "HX Edit.app"), "models"]) == 0
    assert hxedit.find_hx_edit() == tmp_path / "HX Edit.app"
def test_cli_describe_writes_three_snapshots(
    tmp_path: Path,
    dataset_path: Path,
    schema_path: Path,
    template_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The whole describe path, from the LLM's answer to snapshots in the file."""
    preset_path = tmp_path / "snapshots.hlx"
    monkeypatch.setattr(
        "helixgen.llm.request.urlopen",
        _fake_ollama(
            {"title": "Three Ways", "blocks": ["Horizon Drive", "Brit Plexi Brt", "Glitz"]},
            {"Drive": 0.4},
            snapshots=[
                {
                    "name": "Clean",
                    "blocks": {
                        "block1": {"enabled": False},
                        "block2": {"parameters": {"Drive": 0.2}},
                    },
                },
                {"name": "Crunch", "blocks": {"block1": {"enabled": False}}},
                {"name": "High Gain", "blocks": {"block2": {"parameters": {"Drive": 0.8}}}},
            ],
        ),
    )

    exit_code = cli.main(
        [
            "--dataset", str(dataset_path),
            "describe", "Versatile rock",
            "--llm-backend", "ollama",
            "--schema", str(schema_path),
            "--template", str(template_path),
            "--output", str(preset_path),
        ]
    )
    assert exit_code == 0

    tone = json.loads(preset_path.read_text(encoding="utf-8"))["data"]["tone"]
    assert [tone[f"snapshot{n}"]["@name"] for n in range(3)] == ["Clean", "Crunch", "High Gain"]
    assert [tone[f"snapshot{n}"]["blocks"]["dsp0"]["block0"] for n in range(3)] == [
        False, False, True,
    ]
    drive = [tone[f"snapshot{n}"]["controllers"]["dsp0"]["block1"]["Drive"]["@value"] for n in range(3)]
    assert drive[0] == pytest.approx(0.2)
    assert drive[2] == pytest.approx(0.8)

    capsys.readouterr()
    assert cli.main(["inspect", str(preset_path), "--dataset", str(dataset_path)]) == 0
    out = capsys.readouterr().out
    assert "│ Block" in out
    assert "Clean" in out and "High Gain" in out
    assert "│   Drive" in out
