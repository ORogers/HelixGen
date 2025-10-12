from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hlxgen.dataset import ModelCatalog
from hlxgen.llm import (
    _CHAIN_SCHEMA,
    _FEWSHOT_EXAMPLES,
    _compose_prompt,
    _normalize_ollama_endpoint,
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

    assert "Carefully review the catalog snapshot" in prompt
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

    def fake_structured(prompt: str, model_name: str, endpoint: str) -> dict[str, str]:
        _ = prompt, model_name, endpoint
        return [  # type: ignore[return-value]
            {
                "internal_name": "HD2_DistClean",
                "params": {"Clean": 1.0},
            }
        ]

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_structured)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="clean tone",
            catalog=catalog,
            model_name="fake-model",
        )

    assert "JSON object" in str(excinfo.value)


def test_generate_chain_converts_minimal_spec(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_structured(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        return {
            "title": "Clean Tone",
            "blocks": ["Horizon Drive", "Transistor Tape"],
        }

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_structured)

    chain = generate_chain_from_prompt(
        prompt="clean tone",
        catalog=catalog,
        model_name="fake-model",
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

    def fake_structured(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        return {
            "title": "Sparkly",
            "author": "Tone Bot",
            "description": "Bright and chimy",
            "blocks": ["US Double Nrm"],
        }

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_structured)

    chain = generate_chain_from_prompt(
        prompt="sparkly clean",
        catalog=catalog,
        model_name="fake-model",
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

    def fake_structured(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        return {"title": "Bad", "blocks": [123]}  # type: ignore[list-item]

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_structured)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="bad",
            catalog=catalog,
            model_name="fake-model",
        )

    assert "block entry" in str(excinfo.value).lower()


def test_generate_chain_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def fake_structured(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
        _ = prompt, model_name, endpoint
        return {"title": "Mystery", "blocks": ["NotARealModel"]}

    monkeypatch.setattr("hlxgen.llm._invoke_structured_chain", fake_structured)

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(
            prompt="mystery",
            catalog=catalog,
            model_name="fake-model",
        )

    assert "unknown model" in str(excinfo.value).lower()


def test_normalize_endpoint_strips_generate_path() -> None:
    base = _normalize_ollama_endpoint("http://localhost:11434/api/generate")
    assert base == "http://localhost:11434"

    base = _normalize_ollama_endpoint("http://localhost:11434")
    assert base == "http://localhost:11434"

    base = _normalize_ollama_endpoint("http://localhost:11434/api/generate/")
    assert base == "http://localhost:11434/api/generate"


def test_invoke_structured_chain_falls_back_to_json(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    from hlxgen import llm as llm_module

    class DummyMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class DummyChat:
        def __init__(self, model: str, base_url: str, temperature: int) -> None:
            self.model = model
            self.base_url = base_url
            self.temperature = temperature

        def with_structured_output(self, schema: Any) -> Any:  # pragma: no cover - fallback path
            _ = schema
            raise NotImplementedError

        def invoke(self, prompt: str) -> DummyMessage:
            assert "Return EXACTLY one JSON object" in prompt
            payload = json.dumps({"title": "Fallback", "blocks": ["Horizon Drive"]})
            return DummyMessage(payload)

    monkeypatch.setattr(llm_module, "ChatOllama", DummyChat)

    catalog = ModelCatalog(dataset_path)
    full_prompt = llm_module._compose_prompt("clean tone", catalog)

    result = llm_module._invoke_structured_chain(
        prompt=full_prompt,
        model_name="dummy",
        endpoint="http://localhost:11434/api/generate",
    )

    assert result == {"title": "Fallback", "blocks": ["Horizon Drive"]}
