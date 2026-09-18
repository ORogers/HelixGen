import json
import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib import error, request

from .dataset import (
    ModelCatalog,
    ModelCatalogError,
    ModelDefinition,
    ParameterDefinition,
)


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

# Block names below must be display names that exist in helix_model_information.json:
# the prompt penalises the model for inventing names, so the demonstrations have to
# be drawn from the catalog too. Ordering follows the signal-chain rule stated in the
# instructions (dynamics -> drive -> modulation -> amp -> cab -> delay -> reverb).
_FEWSHOT_EXAMPLES = [
    {
        "goal": "Classic rock crunch rhythm with tight low end",
        "response": {
            "title": "Arena Crunch",
            "blocks": [
                "LA Studio Comp",
                "Scream 808",
                "Brit Plexi Nrm",
                "4x12 Greenback 25",
                "63 Spring Reverb",
            ],
        },
    },
    {
        "goal": "Wide ambient clean tone with shimmer and delay",
        "response": {
            "title": "Shimmering Skies",
            "blocks": [
                "Deluxe Comp",
                "70s Chorus",
                "US Double Nrm",
                "2x12 Double C12N",
                "Vintage Digital",
                "Shimmer",
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

_OPENAI_CLIENT: Any | None = None

#: Ceiling on a single Ollama round trip. Generous, because a large local model
#: legitimately takes minutes on one call - but never unbounded: without it a
#: stalled server blocks the caller forever, which in the desktop UI meant a
#: worker thread outliving its window and aborting the process at teardown.
OLLAMA_TIMEOUT_SECONDS = 600


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    llm_model: str,
    endpoint: str = "http://localhost:11434/api/generate",
    *,
    backend: str = "ollama",
    openai_model: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt.

    ``on_progress``, when provided, is called with the same human-readable
    status messages that are otherwise only sent to ``logger`` - one per LLM
    round trip. It is optional and purely additive: omitting it reproduces
    today's CLI behaviour exactly.
    """

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    call_llm = _build_llm_caller(
        backend=backend,
        default_model=llm_model,
        endpoint=endpoint,
        openai_model=openai_model,
    )
    logger.info("Requesting initial block selection for prompt: %s", prompt)
    _report("Requesting initial block selection...")
    full_prompt = _compose_prompt(prompt, catalog)
    response_text = call_llm(full_prompt)
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
    _report(
        f"Selected {len(resolved_models)} block(s): "
        + ", ".join(model.display_name for model in resolved_models)
    )

    if resolved_models:
        _populate_block_parameters_with_llm(
            prompt=prompt,
            call_llm=call_llm,
            chain=chain,
            models=resolved_models,
            on_progress=on_progress,
        )

    _report("Chain generation complete.")
    return chain


def _build_llm_caller(
    *,
    backend: str,
    default_model: str,
    endpoint: str,
    openai_model: str | None,
) -> Callable[[str], str]:
    """Return a callable that executes prompts against the requested LLM backend."""

    backend_key = (backend or "ollama").strip().lower()
    if backend_key == "openai":
        model_name = (openai_model or default_model or "").strip()
        if not model_name:
            raise LLMGenerationError("OpenAI backend requires a model name")
        client = _get_openai_client()

        def _call(prompt: str) -> str:
            return _call_openai(model_name, prompt, client=client)

        return _call

    if backend_key in ("ollama", ""):

        def _call(prompt: str) -> str:
            return _call_ollama(endpoint, default_model, prompt)

        return _call

    raise LLMGenerationError(f"Unsupported LLM backend '{backend}'")


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
        "useful 1-8 blocks. Try to reason why each block would be included to match the user request tone"
        "The chain MUST include an amp and a cab and a reverb. Provide only the title and ordered list "
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
        with request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = ""
        try:
            raw_detail = exc.read().decode("utf-8") if exc.fp else ""
        except (OSError, ValueError):  # pragma: no cover - protective fallback
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


def _call_openai(model_name: str, prompt: str, *, client: Any | None = None) -> str:
    client = client or _get_openai_client()
    try:
        response = client.responses.create(
            model=model_name,
            input=prompt,
        )
    except Exception as exc:  # pragma: no cover - network errors not hit in tests
        raise LLMGenerationError(f"OpenAI request failed: {exc}") from exc
    text = _extract_text_from_openai_response(response)
    if not text:
        raise LLMGenerationError("OpenAI response did not contain text output")
    return text


def _extract_text_from_openai_response(response: Any) -> str | None:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    output = getattr(response, "output", None)
    if isinstance(output, list):
        collected: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text_value = part.get("text")
                    else:
                        text_value = part.get("text") if isinstance(part, dict) else None
                    if isinstance(text_value, str):
                        collected.append(text_value)
            elif isinstance(content, dict):
                text_value = content.get("text")
                if isinstance(text_value, str):
                    collected.append(text_value)
        if collected:
            return "".join(collected).strip()

    if hasattr(response, "choices"):
        choices = response.choices
        if isinstance(choices, list) and choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                content = getattr(message, "content", None)
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    parts: list[str] = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part_text = part.get("text")
                            if isinstance(part_text, str):
                                parts.append(part_text)
                    if parts:
                        return "".join(parts).strip()

    if hasattr(response, "model_dump"):
        try:
            data = response.model_dump()
        except Exception:  # noqa: BLE001 # pragma: no cover - third-party SDK object
            data = None
        if isinstance(data, dict):
            output_text = data.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text
            outputs = data.get("output")
            if isinstance(outputs, list):
                parts = []
                for item in outputs:
                    if isinstance(item, dict):
                        content = item.get("content")
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict):
                                    value = part.get("text")
                                    if isinstance(value, str):
                                        parts.append(value)
                if parts:
                    return "".join(parts).strip()
    return None


def _get_openai_client() -> Any:
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is not None:
        return _OPENAI_CLIENT
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - import failure tested indirectly
        raise LLMGenerationError(
            "OpenAI backend requires the 'openai' package. Install it to use --llm-backend openai."
        ) from exc
    api_key = _ensure_openai_api_key()
    try:
        _OPENAI_CLIENT = OpenAI(api_key=api_key)
    except Exception as exc:  # pragma: no cover - defensive
        raise LLMGenerationError(f"Failed to initialize OpenAI client: {exc}") from exc
    return _OPENAI_CLIENT


def _ensure_openai_api_key() -> str:
    existing = os.getenv("OPENAI_API_KEY")
    if existing:
        return existing
    loaded = _load_dotenv_value("OPENAI_API_KEY")
    if loaded:
        os.environ.setdefault("OPENAI_API_KEY", loaded)
        return loaded
    raise LLMGenerationError(
        "OpenAI backend selected but OPENAI_API_KEY was not found in the environment or .env file."
    )


def _load_dotenv_value(var_name: str) -> str | None:
    for env_path in _iter_candidate_dotenv_paths():
        if not env_path:
            continue
        try:
            with env_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    parsed = _parse_dotenv_line(raw_line)
                    if not parsed:
                        continue
                    key, value = parsed
                    if key == var_name:
                        return value
        except FileNotFoundError:
            continue
        except OSError:  # pragma: no cover - unlikely on local files
            continue
    return None


def _iter_candidate_dotenv_paths() -> Iterable[Path]:
    seen: set[Path] = set()
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        candidate = directory / ".env"
        if candidate not in seen:
            seen.add(candidate)
            yield candidate
    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parent
    for extra in (package_root / ".env", repo_root / ".env"):
        if extra not in seen:
            seen.add(extra)
            yield extra


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip().strip('"').strip("'")
    return key, value


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
    except json.JSONDecodeError as decode_error:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMGenerationError(
                "Could not locate JSON object in LLM response"
            ) from decode_error
        snippet = cleaned[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise LLMGenerationError("LLM response did not contain valid JSON") from exc


def _populate_block_parameters_with_llm(
    prompt: str,
    call_llm: Callable[[str], str],
    chain: dict[str, Any],
    models: list[ModelDefinition],
    on_progress: Callable[[str], None] | None = None,
) -> None:
    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    chain_title = chain.get("meta", {}).get("name") or chain.get("title") or "Generated Tone"
    ordered_block_names = [model.display_name for model in models]

    for index, (block_spec, model) in enumerate(
        zip(chain["blocks"], models, strict=True)
    ):
        if not isinstance(block_spec, dict):
            continue

        parameter_summary = _summarize_parameters(model)
        category_label = (model.category or "").lower()
        if "cab" in category_label:
            logger.info(
                "Skipping parameter selection for cab block '%s'; using defaults.",
                model.display_name,
            )
            _report(f"Skipping '{model.display_name}' (cab defaults used).")
            continue
        if not parameter_summary:
            logger.info("No adjustable parameters found for block '%s'", model.display_name)
            _report(f"No adjustable parameters for '{model.display_name}'.")
            continue

        logger.info(
            "Selecting parameters for block %d/%d: %s",
            index + 1,
            len(models),
            model.display_name,
        )
        _report(
            f"Setting parameters for block {index + 1}/{len(models)}: "
            f"{model.display_name}..."
        )

        parameter_prompt = _compose_parameter_prompt(
            user_prompt=prompt,
            chain_title=chain_title,
            chain_blocks=ordered_block_names,
            model=model,
            parameter_summary=parameter_summary,
            position=index,
        )

        response_text = call_llm(parameter_prompt)
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
            _report(f"Parameters set for '{model.display_name}'.")
        else:
            logger.info("LLM did not specify parameters for '%s'; defaults will be used.", model.display_name)
            _report(f"Using defaults for '{model.display_name}'.")


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
        "Don't set the gain or drive on distortion pedals, compressors or amps very high. distortion pedals will be used in "
        "conjunction with amps to get a high gain sound."
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
    params = parsed.get("parameters", parsed)
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
