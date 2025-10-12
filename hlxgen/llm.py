from __future__ import annotations

import json
from typing import Any
from urllib import error, request

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


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    model_name: str,
    endpoint: str = "http://localhost:11434/api/generate",
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt."""

    full_prompt = _compose_prompt(prompt, catalog)
    response_text = _call_ollama(endpoint, model_name, full_prompt)
    spec = _parse_chain(response_text)
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
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)
    instructions = (
        "You are a tone designer that builds signal chains for the Line 6 Helix. "
        "Return EXACTLY one JSON object compatible with hlxgen and nothing else. "
        "Do not wrap the object in an array or add leading text—the response must "
        "begin with '{' and end with '}'. The object must include a 'title' string "
        "and a 'blocks' array. Each entry in 'blocks' must be the display name of a "
        "model listed below. Select only the blocks that directly support the "
        "requested tone; avoid unrelated effects and limit the chain to the most "
        "useful 1-8 blocks. The chain MUST include an amp and a cab and a reverb. Provide only the title and ordered list "
        "of block names—parameter values will be filled automatically."
        "The order should be as such, dynamics -> drive/distorition -> modulation-> amp -> cab -> delay -> reverb"
        "You do not need to select a pedal from at catagory if you don't think it will fit. I.e a distorion is not always needed."
        "The JSON must " 
        "validate against the exact schema provided. Do not include markdown fences or "
        "commentary—output strictly JSON."
        "You will be strongly pinalized for returning block names that are not in the dataset"
    )
    summary_text = json.dumps(models_summary, indent=2)
    return (
        f"{instructions}\n\n"
        f"Required JSON schema:\n{schema_json}\n\n"
        f"Example chain structure:\n{example_json}\n\n"
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
        grouped.setdefault(category, []).append(info)
    return grouped


def _call_ollama(endpoint: str, model_name: str, prompt: str) -> str:
    payload = json.dumps({"model": model_name, "prompt": prompt, "stream": False}).encode(
        "utf-8"
    )
    print(payload)
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
    cleaned = text.strip()
    if not cleaned:
        raise LLMGenerationError("LLM response was empty")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMGenerationError("Could not locate JSON object in LLM response")
        snippet = cleaned[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise LLMGenerationError("LLM response did not contain valid JSON") from exc
