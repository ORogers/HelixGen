from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlxgen.dataset import ModelCatalog
from hlxgen.llm import (
    _CHAIN_SCHEMA,
    _FEWSHOT_EXAMPLES,
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

    assert "Select only the blocks that directly support the requested tone" in prompt
    assert "Available models by category" in prompt
    assert '"name": "Horizon Drive"' in prompt
    assert '"key_parameters": [' in prompt


def test_compose_prompt_includes_fewshot_examples(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Ambient", catalog)

    for index, sample in enumerate(_FEWSHOT_EXAMPLES, start=1):
        assert f"Example {index} — user goal: {sample['goal']}" in prompt
        rendered = json.dumps(sample["response"], indent=2)
        assert rendered in prompt


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
            llm_model="fake-model",
        )

    assert "JSON object" in str(excinfo.value)


def test_generate_chain_converts_minimal_spec(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name
        if "Available parameters:" in prompt:
            return json.dumps({"parameters": {}})
        return json.dumps(
            {
                "title": "Clean Tone",
                "blocks": ["Horizon Drive", "Transistor Tape"],
            }
        )

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    chain = generate_chain_from_prompt(
        prompt="clean tone",
        catalog=catalog,
        llm_model="fake-model",
    )

    assert chain["meta"]["name"] == "Clean Tone"
    assert chain["blocks"] == [
        {"model": "Horizon Drive"},
        {"model": "Transistor Tape"},
    ]


def test_generate_chain_includes_optional_metadata(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name
        if "Available parameters:" in prompt:
            return json.dumps({"parameters": {}})
        return json.dumps(
            {
                "title": "Sparkly",
                "author": "Tone Bot",
                "description": "Bright and chimy",
                "blocks": ["US Double Nrm"],
            }
        )

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    chain = generate_chain_from_prompt(
        prompt="sparkly clean",
        catalog=catalog,
        llm_model="fake-model",
    )

    assert chain["meta"] == {
        "name": "Sparkly",
        "author": "Tone Bot",
        "description": "Bright and chimy",
    }


def test_generate_chain_rejects_non_string_block(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name
        if "Available parameters:" in prompt:
            return json.dumps({"parameters": {}})
        return json.dumps({"title": "Bad", "blocks": [123]})

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="bad",
            catalog=catalog,
            llm_model="fake-model",
        )

    assert "block entry" in str(excinfo.value).lower()


def test_generate_chain_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name
        if "Available parameters:" in prompt:
            return json.dumps({"parameters": {}})
        return json.dumps({"title": "Mystery", "blocks": ["NotARealModel"]})

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="mystery",
            catalog=catalog,
            llm_model="fake-model",
        )

    assert "unknown model" in str(excinfo.value).lower()


def test_cab_parameters_use_defaults(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        _ = endpoint, model_name
        if "Available parameters:" in prompt:
            pytest.fail("Cab blocks should not trigger parameter selection.")
        return json.dumps(
            {
                "title": "Cab Only",
                "blocks": ["2x12 Blue Bell"],
            }
        )

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)

    chain = generate_chain_from_prompt(
        prompt="just a cab",
        catalog=catalog,
        llm_model="fake-model",
    )

    assert chain["blocks"] == [{"model": "2x12 Blue Bell"}]
    assert "parameters" not in chain["blocks"][0]
