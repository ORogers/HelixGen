from __future__ import annotations

import json
from typing import Any
from urllib import error, request

from .dataset import ModelCatalog, ModelDefinition


class LLMGenerationError(RuntimeError):
    """Raised when an Ollama-backed LLM response cannot be parsed."""


_CHAIN_SCHEMA = {
    "type": "object",
    "required": ["meta", "global", "input", "output", "blocks"],
    "additionalProperties": False,
    "properties": {
        "meta": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "author": {"type": "string"},
                "description": {"type": "string"},
            },
        },
        "global": {"type": "object"},
        "input": {
            "type": "object",
            "required": ["@model"],
            "properties": {"@model": {"type": "string"}, "@input": {"type": "integer"}},
        },
        "output": {
            "type": "object",
            "required": ["@model"],
            "properties": {"@model": {"type": "string"}, "@output": {"type": "integer"}},
        },
        "blocks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["model"],
                "properties": {
                    "model": {"type": "string"},
                    "path": {"type": "integer"},
                    "position": {"type": "integer"},
                    "type": {"type": "integer"},
                    "parameters": {"type": "object"},
                },
            },
        },
    },
}


_EXAMPLE_CHAIN = {
    "meta": {"name": "Example Tone", "author": "Ollama"},
    "global": {"@tempo": 120.0},
    "input": {"@model": "HelixStomp_AppDSPFlowInput", "@input": 1},
    "output": {"@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1},
    "blocks": [
        {
            "model": "Horizon Drive",
            "path": 0,
            "position": 0,
            "type": 0,
            "parameters": {"Drive": 2.0, "Bright": 0.2},
        },
        {
            "model": "HD2_DlyTransistorTape",
            "path": 0,
            "position": 1,
            "type": 2,
        },
    ],
}


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    model_name: str,
    endpoint: str = "http://localhost:11434/api/generate",
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt."""

    full_prompt = _compose_prompt(prompt, catalog)
    response_text = _call_ollama(endpoint, model_name, full_prompt)
    chain = _parse_chain(response_text)
    if not isinstance(chain, dict):
        raise LLMGenerationError("LLM response did not return a JSON object")
    blocks = chain.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise LLMGenerationError("LLM response must include a non-empty 'blocks' list")
    return chain


def _compose_prompt(user_prompt: str, catalog: ModelCatalog) -> str:
    models_summary = _summarize_catalog(catalog)
    example_json = json.dumps(_EXAMPLE_CHAIN, indent=2)
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)
    instructions = (
        "You are a tone designer that builds signal chains for the Line 6 Helix. "
        "Return a single JSON object compatible with hlxgen. The object must include "
        "the top-level keys 'meta', 'global', 'input', 'output', and 'blocks'. "
        "The 'blocks' array must contain entries whose 'model' value comes from the "
        "available models list provided below. The JSON must validate against the "
        "exact schema provided. When unsure about parameter values, omit them to fall "
        "back to dataset defaults. Do not include markdown fences or commentary—output "
        "strictly JSON."
    )
    summary_text = json.dumps(models_summary, indent=2)
    return (
        f"{instructions}\n\n"
        f"Required JSON schema:\n{schema_json}\n\n"
        f"Example chain structure:\n{example_json}\n\n"
        f"Available models (display_name, internal_name, category, parameters):\n{summary_text}\n\n"
        f"User goal: {user_prompt.strip()}\n"
        "Respond with valid JSON only."
    )


def _summarize_catalog(catalog: ModelCatalog) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for model in sorted(catalog.models(), key=lambda m: m.display_name.lower()):
        summary.append(
            {
                "display_name": model.display_name,
                "internal_name": model.internal_name,
                "category": model.category or "",
                "parameters": _parameter_sample(model),
            }
        )
    return summary


def _parameter_sample(model: ModelDefinition) -> list[str]:
    names = [name for name in model.parameters.keys() if not name.startswith("@")]
    names.sort()
    if len(names) > 12:
        names = names[:12]
        names.append("…")
    return names


def _call_ollama(endpoint: str, model_name: str, prompt: str) -> str:
    payload = json.dumps({"model": model_name, "prompt": prompt, "stream": False}).encode(
        "utf-8"
    )
    req = request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = ""
        try:
            raw_detail = exc.read().decode("utf-8") if exc.fp else ""
        except Exception:  # pragma: no cover - protective fallback
            raw_detail = ""
        if raw_detail:
            try:
                decoded = json.loads(raw_detail)
            except json.JSONDecodeError:
                detail = raw_detail.strip()
            else:
                if isinstance(decoded, dict):
                    detail = str(decoded.get("error") or raw_detail.strip())
                else:
                    detail = raw_detail.strip()
        message = f"Ollama returned HTTP {exc.code}"
        if exc.reason:
            message += f" ({exc.reason})"
        if detail:
            message += f": {detail}"
        else:
            message += "."
        if exc.code == 404:
            message += (
                " Ensure the endpoint URL includes the /api/generate path or adjust it via "
                "--ollama-endpoint."
            )
        raise LLMGenerationError(message) from exc
    except error.URLError as exc:
        raise LLMGenerationError(f"Failed to reach Ollama endpoint {endpoint}: {exc}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError("Ollama returned invalid JSON") from exc
    if isinstance(parsed, dict) and parsed.get("error"):
        raise LLMGenerationError(str(parsed["error"]))
    if not isinstance(parsed, dict) or "response" not in parsed:
        raise LLMGenerationError("Unexpected response from Ollama")
    response_text = parsed.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        raise LLMGenerationError("Ollama response did not contain text output")
    return response_text


def _parse_chain(response_text: str) -> Any:
    text = response_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMGenerationError("Could not locate JSON object in LLM response")
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError("LLM response did not contain valid JSON") from exc
