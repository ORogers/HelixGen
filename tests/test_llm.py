from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlxgen.dataset import ModelCatalog
from hlxgen.llm import (
    _CHAIN_SCHEMA,
    _compose_prompt,
    generate_chain_from_prompt,
    LLMGenerationError,
)


def test_compose_prompt_includes_schema(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Create a bluesy crunch", catalog)
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)

    assert "Required JSON schema:" in prompt
    assert schema_json in prompt
    assert "Return EXACTLY one JSON object" in prompt
    assert "begin with '{'" in prompt


def test_compose_prompt_includes_model_details(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Clean tone", catalog)

    assert '"based_on": "Horizon Devices Precision Drive incl Gate Range"' in prompt
    assert '"parameters": {' in prompt
    assert '"Drive": {' in prompt
    assert '"default": 0.25' in prompt


def test_generate_chain_requires_object(monkeypatch: pytest.MonkeyPatch, dataset_path: Path):
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name, prompt
        return json.dumps(
            [
                {
                    "internal_name": "HD2_DistClean",
                    "params": {"Clean": 1.0},
                }
            ]
        )

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="clean tone",
            catalog=catalog,
            model_name="fake-model",
        )

    assert "JSON object" in str(excinfo.value)
