from __future__ import annotations

import json
from typing import Any
from urllib import parse

try:  # pragma: no cover - optional dependency resolution
    from langchain_community.chat_models import ChatOllama
    from langchain_core.pydantic_v1 import BaseModel, Field
except ModuleNotFoundError:  # pragma: no cover - fallback when langchain is absent
    ChatOllama = None  # type: ignore

    class BaseModel:  # type: ignore
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - stub
            raise RuntimeError(
                "LangChain is required for describe generation; install langchain-core and langchain-community."
            )

        def dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(
                "LangChain is required for describe generation; install langchain-core and langchain-community."
            )

    def Field(default: Any, **_: Any) -> Any:  # type: ignore
        return default

from .dataset import ModelCatalog, ModelCatalogError


class LLMGenerationError(RuntimeError):
    """Raised when an Ollama-backed LLM response cannot be parsed."""


_CHAIN_SCHEMA = {
    "type": "object",
    "required": ["title", "blocks"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "author": {"type": "string"},
        "description": {"type": "string"},
        "blocks": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}


_EXAMPLE_CHAIN = {
    "title": "Example Tone",
    "blocks": [
        "Horizon Drive",
        "Transistor Tape",
    ],
}


_FEWSHOT_EXAMPLES: list[dict[str, Any]] = [
    {
        "goal": "Bright Nashville clean rhythm with subtle compression and slapback",
        "response": {
            "title": "Nashville Sparkle",
            "blocks": [
                "US Deluxe Vib",
                "LA Studio Comp",
                "Transistor Tape",
            ],
        },
    },
    {
        "goal": "Modern worship ambience with lush modulation and spacious reverb",
        "response": {
            "title": "Shimmering Atmosphere",
            "blocks": [
                "US Double Nrm",
                "Elephant Man",
                "Plate Reverb",
            ],
        },
    },
    {
        "goal": "Tight high-gain rhythm tone with boosted attack and noise control",
        "response": {
            "title": "Punchy Recto Rhythm",
            "blocks": [
                "Scream 808",
                "Cali Rectifire",
                "Hard Gate",
            ],
        },
    },
]


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    model_name: str,
    endpoint: str = "http://localhost:11434/api/generate",
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt."""

    full_prompt = _compose_prompt(prompt, catalog)
    spec = _invoke_structured_chain(full_prompt, model_name, endpoint)
    if not isinstance(spec, dict):
        raise LLMGenerationError(
            "LLM response must be a JSON object that matches the chain schema"
        )
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        raise LLMGenerationError("LLM response must include a non-empty 'title' string")

    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise LLMGenerationError("LLM response must include a non-empty 'blocks' list")

    chain: dict[str, Any] = {
        "meta": {"name": title.strip()},
        "blocks": [],
    }

    author = spec.get("author")
    if isinstance(author, str) and author.strip():
        chain["meta"]["author"] = author.strip()

    description = spec.get("description")
    if isinstance(description, str) and description.strip():
        chain["meta"]["description"] = description.strip()

    for entry in blocks:
        if not isinstance(entry, str) or not entry.strip():
            raise LLMGenerationError("Each block entry must be the name of a model as a string")
        model_name = entry.strip()
        try:
            model = catalog.get(model_name)
        except ModelCatalogError as exc:
            raise LLMGenerationError(f"Unknown model '{model_name}' in LLM response") from exc
        chain["blocks"].append({"model": model.display_name})

    return chain


def _compose_prompt(user_prompt: str, catalog: ModelCatalog) -> str:
    models_summary = _summarize_catalog(catalog)
    example_json = json.dumps(_EXAMPLE_CHAIN, indent=2)
    fewshot_sections = []
    for index, sample in enumerate(_FEWSHOT_EXAMPLES, start=1):
        sample_json = json.dumps(sample["response"], indent=2)
        fewshot_sections.append(
            "Example {index} — user goal: {goal}\nValid JSON response:\n{response}".format(
                index=index, goal=sample["goal"], response=sample_json
            )
        )
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)
    instructions = (
        "You are a tone designer that builds signal chains for the Line 6 Helix. "
        "Carefully review the catalog snapshot and silently compare candidate blocks "
        "before deciding which ones to use. Return EXACTLY one JSON object compatible "
        "with hlxgen and nothing else. Do not wrap the object in an array or add "
        "leading text—the response must begin with '{' and end with '}'. The object "
        "must include a 'title' string and a 'blocks' array. Each entry in 'blocks' "
        "must be the display name of a model listed below. Select only the blocks that "
        "directly support the requested tone; avoid unrelated effects and limit the "
        "chain to the most useful 1-8 blocks in musical order. Provide only the title "
        "and ordered list of block names—parameter values will be filled automatically. "
        "The JSON must validate against the exact schema provided. Do not include "
        "markdown fences or commentary—output strictly JSON."
    )
    summary_text = json.dumps(models_summary, indent=2)
    return (
        f"{instructions}\n\n"
        f"Required JSON schema:\n{schema_json}\n\n"
        f"Example chain structure:\n{example_json}\n\n"
        f"Example conversions from goals to JSON:\n{chr(10).join(fewshot_sections)}\n\n"
        f"Available models by category (ordered, include only relevant blocks):\n{summary_text}\n\n"
        f"User goal: {user_prompt.strip()}\n"
        "Respond with valid JSON only."
    )


def _summarize_catalog(catalog: ModelCatalog) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for model in sorted(catalog.models(), key=lambda m: m.display_name.lower()):
        category = model.category or "Uncategorized"
        info: dict[str, Any] = {
            "name": model.display_name,
            "internal_name": model.internal_name,
        }
        if model.based_on:
            info["based_on"] = model.based_on
        key_params = list(sorted(model.parameters.keys()))
        if key_params:
            info["key_parameters"] = key_params[:5]
        grouped.setdefault(category, []).append(info)
    return grouped


class _ChainSuggestion(BaseModel):
    title: str = Field(..., min_length=1)
    blocks: list[str] = Field(..., min_items=1, max_items=8)
    author: str | None = None
    description: str | None = None


def _invoke_structured_chain(prompt: str, model_name: str, endpoint: str) -> dict[str, Any]:
    if ChatOllama is None:
        raise LLMGenerationError(
            "LangChain is not installed. Install langchain-core and langchain-community to use describe."
        )
    base_url = _normalize_ollama_endpoint(endpoint)
    try:
        llm = ChatOllama(model=model_name, base_url=base_url, temperature=0)
    except Exception as exc:  # pragma: no cover - defensive
        raise LLMGenerationError(f"Failed to initialize Ollama client: {exc}") from exc

    try:
        structured_llm = llm.with_structured_output(_ChainSuggestion)
    except NotImplementedError:
        return _invoke_raw_json(llm, prompt)
    except Exception as exc:  # pragma: no cover - defensive
        raise LLMGenerationError(f"Failed to initialize structured output: {exc}") from exc
    try:
        result = structured_llm.invoke(prompt)
    except Exception as exc:
        raise LLMGenerationError(f"Ollama generation failed: {exc}") from exc

    if isinstance(result, _ChainSuggestion):
        return result.dict(exclude_none=True)
    if isinstance(result, dict):
        return result
    return _coerce_structured_result(result)


def _coerce_structured_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "dict"):
        try:
            return result.dict(exclude_none=True)  # type: ignore[call-arg]
        except Exception as exc:  # pragma: no cover - defensive
            raise LLMGenerationError(
                f"Unexpected structured output from LangChain/Ollama: {exc}"
            ) from exc
    raise LLMGenerationError("Unexpected structured output from LangChain/Ollama")


def _normalize_ollama_endpoint(endpoint: str) -> str:
    parsed = parse.urlparse(endpoint)
    path = parsed.path or ""
    if path.endswith("/api/generate"):
        path = path[: -len("/api/generate")]
    if not path:
        path = "/"
    normalized = parsed._replace(path=path)
    base = parse.urlunparse(normalized)
    return base.rstrip("/")


def _invoke_raw_json(llm: Any, prompt: str) -> dict[str, Any]:
    try:
        raw = llm.invoke(prompt)
    except Exception as exc:  # pragma: no cover - defensive
        raise LLMGenerationError(f"Ollama generation failed: {exc}") from exc

    text = _coerce_message_text(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError(
            "LLM response was not valid JSON when structured output was unavailable"
        ) from exc
    if not isinstance(parsed, dict):
        raise LLMGenerationError(
            "LLM response must be a JSON object that matches the chain schema"
        )
    return parsed


def _coerce_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # pragma: no cover - defensive list handling
        text_parts: list[str] = []
        for chunk in content:
            if isinstance(chunk, str):
                text_parts.append(chunk)
            elif isinstance(chunk, dict) and "text" in chunk:
                text = chunk["text"]
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    raise LLMGenerationError("Unable to interpret Ollama response text")
