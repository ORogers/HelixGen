import json
from pathlib import Path

import pytest

from hlxgen import resources


@pytest.fixture(autouse=True)
def _no_ollama_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the network: the thinking-capability probe would otherwise
    ask a real Ollama server. Decide from the model name instead."""
    from hlxgen import llm

    monkeypatch.setattr(
        llm,
        "_ollama_supports_thinking",
        lambda endpoint, model_name: llm._is_levelled_ollama_model(model_name),
    )


@pytest.fixture(scope="session")
def dataset_path() -> Path:
    """The real model catalog, from wherever hlxgen is installed.

    Resolved through ``hlxgen.resources`` rather than the repo root, so the
    suite passes against an installed wheel exactly as it does in a clone.
    """
    return resources.dataset_path()


@pytest.fixture(scope="session")
def template_path() -> Path:
    return resources.template_path()


@pytest.fixture(scope="session")
def template_data(template_path: Path) -> dict[str, object]:
    with template_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def schema_path(tmp_path: Path) -> Path:
    schema: dict[str, object] = {
        "type": "object",
        "required": ["data"],
        "properties": {
            "data": {
                "type": "object",
                "required": ["meta", "tone"],
                "properties": {
                    "meta": {"type": "object"},
                    "tone": {
                        "type": "object",
                        "required": ["global", "dsp0"],
                        "properties": {
                            "global": {"type": "object"},
                            "dsp0": {"type": "object"},
                        },
                    },
                },
            }
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def horizon_chain() -> dict[str, object]:
    return {
        "meta": {
            "name": "Precision Drive Patch",
            "application": "HX Edit",
            "appversion": 58851328,
        },
        "global": {"@tempo": 110.0},
        "input": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 1},
        "output": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1},
        "blocks": [
            {
                "id": "drive",
                "model": "Horizon Drive",
                "path": 0,
                "position": 0,
                "type": 0,
                "parameters": {
                    "Drive": 2.0,  # exceeds max to exercise clamping behaviour
                    "Bright": 0.2,
                },
            }
        ],
    }
