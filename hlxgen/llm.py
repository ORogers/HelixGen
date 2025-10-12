from __future__ import annotations

import json
import logging
from typing import Any, Iterable
from urllib import error, request

from .dataset import ModelCatalog, ModelCatalogError
from .dataset import ModelDefinition, ParameterDefinition


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

_FEWSHOT_EXAMPLES = [
    {
        "goal": "Classic rock crunch rhythm with tight low end",
        "response": {
            "title": "Arena Crunch",
            "blocks": [
                "LA Studio Comp",
                "Minotaur",
                "Brit Plexi Brt",
                "2x12 Blue Bell",
                "Plate Reverb",
            ],
        },
    },
    {
        "goal": "Wide ambient clean tone with shimmer and delay",
        "response": {
            "title": "Shimmering Skies",
            "blocks": [
                "Deluxe Comp",
                "Jazz Rivet 120",
                "2x12 Match H30",
                "Adriatic Delay",
                "Glitz",
            ],
        },
    },
]

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    llm_model: str,
    endpoint: str = "http://localhost:11434/api/generate",
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt."""

    logger.info("Requesting initial block selection for prompt: %s", prompt)
    full_prompt = _compose_prompt(prompt, catalog)
    response_text = _call_ollama(endpoint, llm_model, full_prompt)
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

    resolved_models: list[ModelDefinition] = []
    for entry in blocks:
        if not isinstance(entry, str) or not entry.strip():
            raise LLMGenerationError("Each block entry must be the name of a model as a string")
        requested_model_name = entry.strip()
        try:
            model = catalog.get(requested_model_name)
        except ModelCatalogError as exc:
            raise LLMGenerationError(
                f"Unknown model '{requested_model_name}' in LLM response"
            ) from exc
        resolved_models.append(model)
        chain["blocks"].append({"model": model.display_name})

    logger.info(
        "LLM selected %d block(s): %s",
        len(resolved_models),
        ", ".join(model.display_name for model in resolved_models),
    )

    if resolved_models:
        _populate_block_parameters_with_llm(
            prompt=prompt,
            llm_model=llm_model,
            endpoint=endpoint,
            chain=chain,
            models=resolved_models,
        )

    return chain


def _compose_prompt(user_prompt: str, catalog: ModelCatalog) -> str:
    models_summary = _summarize_catalog(catalog)
    example_json = json.dumps(_EXAMPLE_CHAIN, indent=2)
    schema_json = json.dumps(_CHAIN_SCHEMA, indent=2)
    fewshot_blocks: list[str] = []
    for index, sample in enumerate(_FEWSHOT_EXAMPLES, start=1):
        rendered = json.dumps(sample["response"], indent=2)
        fewshot_blocks.append(
            f"Example {index} — user goal: {sample['goal']}\n{rendered}"
        )
    fewshot_text = "\n\n".join(fewshot_blocks)
    instructions = (
        "You are a tone designer that builds signal chains for the Line 6 Helix. "
        "Return EXACTLY one JSON object compatible with hlxgen and nothing else. "
        "Do not wrap the object in an array or add leading text—the response must "
        "begin with '{' and end with '}'. The object must include a 'title' string "
        "and a 'blocks' array. Each entry in 'blocks' must be the display name of a "
        "model listed below. Select only the blocks that directly support the "
        "requested tone; avoid unrelated effects and limit the chain to the most "
        "useful 1-8 blocks. The chain MUST include an amp and a cab and a reverb. Provide only the title and ordered list "
        "of block names—parameters will be requested separately, do not include them now."
        "The order should be as such, dynamics -> drive/distorition -> modulation-> amp -> cab -> delay -> reverb"
        "You do not need to select a pedal from at catagory if you don't think it will fit. I.e a distorion is not always needed."
        "The JSON must "
        "validate against the exact schema provided. Do not include markdown fences or "
        "commentary—output strictly JSON."
        "You will be strongly pinalized for returning block names that are not in the dataset"
    )
    summary_text = json.dumps(models_summary, indent=2)
    sections: list[str] = [
        instructions,
        f"Required JSON schema:\n{schema_json}",
        f"Example chain structure:\n{example_json}",
    ]
    if fewshot_text:
        sections.append(fewshot_text)
    sections.append(
        "Available models by category (ordered, include only relevant blocks):\n"
        f"{summary_text}"
    )
    sections.append(f"User goal: {user_prompt.strip()}")
    sections.append("Respond with valid JSON only.")
    return "\n\n".join(sections)

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
        key_parameters = [
            name for name in model.parameter_names() if not name.startswith("@")
        ]
        if key_parameters:
            info["key_parameters"] = key_parameters[:6]
        grouped.setdefault(category, []).append(info)
    return grouped


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
    return _parse_json_response(response_text)


def _parse_json_response(response_text: str) -> Any:
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


def _populate_block_parameters_with_llm(
    prompt: str,
    llm_model: str,
    endpoint: str,
    chain: dict[str, Any],
    models: list[ModelDefinition],
) -> None:
    chain_title = chain.get("meta", {}).get("name") or chain.get("title") or "Generated Tone"
    ordered_block_names = [model.display_name for model in models]

    for index, (block_spec, model) in enumerate(zip(chain["blocks"], models)):
        if not isinstance(block_spec, dict):
            continue

        parameter_summary = _summarize_parameters(model)
        category_label = (model.category or "").lower()
        if "cab" in category_label:
            logger.info(
                "Skipping parameter selection for cab block '%s'; using defaults.",
                model.display_name,
            )
            continue
        if not parameter_summary:
            logger.info("No adjustable parameters found for block '%s'", model.display_name)
            continue

        logger.info(
            "Selecting parameters for block %d/%d: %s",
            index + 1,
            len(models),
            model.display_name,
        )

        parameter_prompt = _compose_parameter_prompt(
            user_prompt=prompt,
            chain_title=chain_title,
            chain_blocks=ordered_block_names,
            model=model,
            parameter_summary=parameter_summary,
            position=index,
        )

        response_text = _call_ollama(endpoint, llm_model, parameter_prompt)
        raw_parameters = _parse_parameter_response(response_text)
        try:
            normalized_parameters = _normalize_parameters(model, raw_parameters)
        except LLMGenerationError:
            raise
        except Exception as exc:  # pragma: no cover - wrap unexpected validation issues
            raise LLMGenerationError(
                f"Failed to normalize parameters for block '{model.display_name}': {exc}"
            ) from exc

        if normalized_parameters:
            block_spec.setdefault("parameters", {}).update(normalized_parameters)
            logger.info(
                "Parameters selected for '%s': %s",
                model.display_name,
                normalized_parameters,
            )
        else:
            logger.info("LLM did not specify parameters for '%s'; defaults will be used.", model.display_name)


def _compose_parameter_prompt(
    user_prompt: str,
    chain_title: str,
    chain_blocks: Iterable[str],
    model: ModelDefinition,
    parameter_summary: list[dict[str, Any]],
    position: int,
) -> str:
    summary_json = json.dumps(parameter_summary, indent=2)
    chain_json = json.dumps(list(chain_blocks), indent=2)

    instructions = (
        "You are configuring parameters for a single block in a Line 6 Helix preset. "
        "Use the player's request and overall chain context to choose musical values. "
        "Respond with EXACTLY one JSON object. The object MUST contain a single key "
        "named 'parameters' whose value is a mapping of parameter names to the chosen values. "
        "Do not include explanatory text or markdown fences. "
        "Choose values that make musical sense and stay within the provided limits. "
        "For enumerated options, return the option label exactly as listed. "
        "For boolean parameters, return true or false. "
        "For continuous parameters, return a numeric value within the inclusive range. "
        "Only include parameters you intend to adjust; omit ones that should stay at their defaults."
    )

    return (
        f"{instructions}\n\n"
        f"Player request: {user_prompt.strip()}\n"
        f"Chain title: {chain_title}\n"
        f"Full chain order: {chain_json}\n"
        f"Target block position: {position + 1}\n"
        f"Target block name: {model.display_name}\n"
        f"Block category: {model.category or 'Uncategorized'}\n"
        f"Reference (based on): {model.based_on or 'Unknown'}\n"
        f"Available parameters:\n{summary_json}\n"
        "Return a JSON object like:\n{\n  \"parameters\": {\n    \"Drive\": 0.65,\n    \"Level\": 0.8\n  }\n}\n"
        "Respond with JSON only."
    )


def _summarize_parameters(model: ModelDefinition) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for name in sorted(model.parameter_names()):
        if name.startswith("@"):
            continue
        definition = model.get_parameter(name)

        entry: dict[str, Any] = {"name": name}
        default = definition.default_value()
        if default is not None:
            entry["default"] = default

        if definition.value_type == 1:
            entry["type"] = "continuous"
            if definition.min_value is not None:
                entry["min"] = definition.min_value
            if definition.max_value is not None:
                entry["max"] = definition.max_value
        elif definition.value_type == 2 or definition.forward_map:
            entry["type"] = "enum"
            options = []
            for raw_value, label in sorted(definition.forward_map.items(), key=lambda item: item[0]):
                try:
                    normalized_value = int(raw_value)
                except ValueError:
                    normalized_value = raw_value
                options.append({"value": normalized_value, "label": label})
            if options:
                entry["options"] = options
            if definition.display_type == "boolean":
                entry["type"] = "boolean"
        else:
            if definition.display_type == "boolean":
                entry["type"] = "boolean"
            else:
                entry["type"] = definition.display_type or "value"

        summary.append(entry)
    return summary


def _parse_parameter_response(response_text: str) -> dict[str, Any]:
    parsed = _parse_json_response(response_text)
    if not isinstance(parsed, dict):
        raise LLMGenerationError("LLM parameter response must be a JSON object.")
    if "parameters" in parsed:
        params = parsed["parameters"]
    else:
        params = parsed
    if not isinstance(params, dict):
        raise LLMGenerationError("LLM parameter response must map parameter names to values.")
    return params


def _normalize_parameters(
    model: ModelDefinition,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, value in parameters.items():
        if name not in model.parameters:
            raise LLMGenerationError(
                f"Parameter '{name}' is not valid for model '{model.display_name}'."
            )
        definition: ParameterDefinition = model.get_parameter(name)
        try:
            normalized[name] = definition.normalize(value)
        except ModelCatalogError as exc:
            raise LLMGenerationError(
                f"Invalid value for parameter '{name}' on model '{model.display_name}': {exc}"
            ) from exc
    return normalized
