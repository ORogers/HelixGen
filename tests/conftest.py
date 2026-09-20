import json
from pathlib import Path

import pytest

from helixgen import resources


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real config file.

    It now holds the user's own backend choice and their OpenAI key, so a test
    reading it would pass or fail differently on every machine, and one writing
    it would overwrite someone's settings.
    """
    monkeypatch.setenv(
        "HELIXGEN_CONFIG_DIR", str(tmp_path_factory.mktemp("config"))
    )


@pytest.fixture(autouse=True)
def _no_ollama_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the network: the thinking-capability probe would otherwise
    ask a real Ollama server. Decide from the model name instead."""
    from helixgen import llm

    monkeypatch.setattr(
        llm,
        "_ollama_supports_thinking",
        lambda endpoint, model_name: llm._is_levelled_ollama_model(model_name),
    )


@pytest.fixture(scope="session")
def dataset_path() -> Path:
    """The real model catalog, from wherever helixgen is installed.

    Resolved through ``helixgen.resources`` rather than the repo root, so the
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


@pytest.fixture(autouse=True)
def _no_live_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach a real backend, whichever one is the default.

    When OpenAI became the default, four tests that stub Ollama's HTTP call
    silently started talking to the real API - and on a machine with a key in
    its environment, one of them passed by spending money. A default is not
    something the suite should be able to notice.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("helixgen.llm.openai_api_key", lambda: None)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "A test tried to reach the OpenAI API. Stub the backend, or pass "
            "--llm-backend ollama if the test is about the Ollama path."
        )

    monkeypatch.setattr("helixgen.llm._get_openai_client", _refuse)
    monkeypatch.setattr("helixgen.llm.verify_openai_api_key", _refuse)
