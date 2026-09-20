import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error, request

from .dataset import (
    CONTINUOUS,
    ModelCatalog,
    ModelCatalogError,
    ModelDefinition,
    ParameterDefinition,
)


class LLMGenerationError(RuntimeError):
    """Raised when an LLM request fails or its response cannot be used."""


# Defaults shared by the CLI (`hlxgen describe`) and the desktop UI (hlxgen_ui), so
# the two can never drift apart on which model or thinking level they start from.
DEFAULT_OLLAMA_MODEL = "gpt-oss:20b"
DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"

#: The OpenAI models offered, most capable first, each with a short description.
OPENAI_MODELS: tuple[tuple[str, str], ...] = (
    ("gpt-5.6-sol", "Flagship"),
    ("gpt-5.6-terra", "Balanced"),
    ("gpt-5.6-luna", "Cost-optimized"),
)
DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

#: Thinking levels, least to most. Every offered OpenAI model supports all of them;
#: Ollama models support a subset (see `supported_reasoning_efforts`).
REASONING_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "low"

#: Context window requested from Ollama. It is raised automatically when a prompt
#: needs more, so a prompt is never silently truncated; it is kept fixed across
#: the calls of one generation because changing it makes Ollama reload the model.
DEFAULT_NUM_CTX = 32768

#: How long Ollama keeps the model loaded after a call, so the next generation
#: skips reloading several gigabytes of weights.
OLLAMA_KEEP_ALIVE = "30m"

#: Ceiling on a single Ollama round trip. Generous, because a large local model
#: legitimately takes minutes on one call - but never unbounded: without it a
#: stalled server blocks the caller forever, which in the desktop UI meant a
#: worker thread outliving its window and aborting the process at teardown.
OLLAMA_TIMEOUT_SECONDS = 600

#: Room left in the context window for the model's reasoning and its answer.
_OUTPUT_HEADROOM_TOKENS = 8192

#: Ollama models whose `think` option takes a level rather than true/false.
_OLLAMA_LEVELLED_THINKING = ("gpt-oss",)
_OLLAMA_LEVELS = ("low", "medium", "high")

#: Ollama models that answer with empty text whenever a response schema is set
#: (seen with gpt-oss on Ollama 0.33, for any schema). They get the JSON shape from
#: the prompt alone, and a rejected answer is re-asked with the reason instead.
_OLLAMA_SCHEMA_UNSUPPORTED = ("gpt-oss",)

# Block names below must be display names that exist in helix_model_information.json:
# the response schema only admits catalog names, so the demonstrations have to be
# drawn from the catalog too. Ordering follows the signal-chain rule stated in the
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

_MAX_BLOCKS = 8

#: An HX Stomp preset carries three snapshots. Each one becomes a distinct usable
#: variation of the tone -- clean / rhythm / lead and the like -- rather than the
#: same sound with blocks muted.
SNAPSHOT_COUNT = 3
#: Longest snapshot name the device displays.
SNAPSHOT_NAME_LIMIT = 10

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


@dataclass(frozen=True)
class LLMRequest:
    """One prompt plus the JSON schema its answer is constrained to."""

    prompt: str
    schema: dict[str, Any]
    #: Identifies the round ("chain" or "parameters"); OpenAI also requires it.
    schema_name: str


LLMCaller = Callable[[LLMRequest], str]


def generate_chain_from_prompt(
    prompt: str,
    catalog: ModelCatalog,
    llm_model: str = DEFAULT_OLLAMA_MODEL,
    endpoint: str = DEFAULT_OLLAMA_ENDPOINT,
    *,
    backend: str = "ollama",
    openai_model: str | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    num_ctx: int = DEFAULT_NUM_CTX,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Return a signal chain dictionary generated from a natural-language prompt.

    Three LLM round trips: one picks the blocks, one sets every block's
    parameters, and one designs the preset's snapshots.
    Each answer is constrained to a JSON schema, and a rejected answer is re-asked
    once with the reason attached before the run fails.

    ``on_progress``, when provided, is called with the same human-readable
    status messages that are otherwise only sent to ``logger``. It is optional
    and purely additive: omitting it reproduces the CLI behaviour exactly.
    """

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    started = time.monotonic()
    call_llm = _build_llm_caller(
        backend=backend,
        default_model=llm_model,
        endpoint=endpoint,
        openai_model=openai_model,
        reasoning_effort=reasoning_effort,
        num_ctx=num_ctx,
    )

    logger.info("Requesting initial block selection for prompt: %s", prompt)
    _report("Requesting initial block selection...")
    round_started = time.monotonic()
    chain_request = LLMRequest(
        prompt=_compose_prompt(prompt, catalog),
        schema=_chain_schema(catalog),
        schema_name="chain",
    )
    chain, resolved_models = _ask(
        call_llm,
        chain_request,
        lambda text: _parse_chain_response(text, catalog),
        on_retry=lambda: _report("Retrying block selection..."),
    )
    elapsed = time.monotonic() - round_started
    names = ", ".join(model.display_name for model in resolved_models)
    logger.info("LLM selected %d block(s) in %.1fs: %s", len(resolved_models), elapsed, names)
    _report(f"Selected {len(resolved_models)} block(s): {names} ({elapsed:.1f}s)")

    _populate_block_parameters_with_llm(
        prompt=prompt,
        call_llm=call_llm,
        chain=chain,
        models=resolved_models,
        on_progress=on_progress,
    )

    _design_snapshots_with_llm(
        prompt=prompt,
        call_llm=call_llm,
        chain=chain,
        models=resolved_models,
        on_progress=on_progress,
    )

    total = time.monotonic() - started
    logger.info("Chain generation complete in %.1fs", total)
    _report(f"Chain generation complete ({total:.1f}s total).")
    return chain


def supported_reasoning_efforts(
    backend: str, model: str, endpoint: str | None = None
) -> tuple[str, ...]:
    """The thinking levels ``model`` accepts on ``backend``, least to most.

    Empty when the model does not think at all. For Ollama, pass ``endpoint`` to
    ask the server what the model can do; without it the answer comes from the
    model's name alone.
    """

    if _normalize_backend(backend) == "openai":
        return REASONING_EFFORTS
    if _is_levelled_ollama_model(model):
        return _OLLAMA_LEVELS
    if endpoint is not None and not _ollama_supports_thinking(endpoint, model):
        return ()
    # Other thinking models on Ollama only switch thinking on ("low") or off.
    return ("none", "low")


def resolve_reasoning_effort(backend: str, model: str, requested: str) -> str:
    """Return ``requested`` if ``model`` supports it, else the nearest level it does."""

    if requested not in REASONING_EFFORTS:
        raise LLMGenerationError(
            f"Unknown thinking level '{requested}'. Choose one of: {', '.join(REASONING_EFFORTS)}"
        )
    return nearest_reasoning_effort(requested, supported_reasoning_efforts(backend, model))


def nearest_reasoning_effort(requested: str, supported: Iterable[str]) -> str:
    """The level in ``supported`` closest to ``requested``; ties go to the lower
    level, because lower is faster. ``requested`` itself when nothing is supported."""

    supported = tuple(supported)
    if not supported or requested in supported:
        return requested
    target = REASONING_EFFORTS.index(requested)
    return min(
        supported,
        key=lambda level: (
            abs(REASONING_EFFORTS.index(level) - target),
            REASONING_EFFORTS.index(level),
        ),
    )


def list_llm_models(backend: str, endpoint: str = DEFAULT_OLLAMA_ENDPOINT) -> list[str]:
    """Models the user can pick for ``backend``.

    OpenAI offers a fixed list (:data:`OPENAI_MODELS`); Ollama lists what is
    installed on the server behind ``endpoint``.
    """

    if _normalize_backend(backend) == "openai":
        return [model_id for model_id, _ in OPENAI_MODELS]
    body = _ollama_request(_ollama_url(endpoint, "/api/tags"), None, timeout=10)
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        raise LLMGenerationError("Unexpected response from Ollama when listing models")
    names = [entry.get("name") for entry in models if isinstance(entry, dict)]
    return sorted(name for name in names if isinstance(name, str) and name)


def _normalize_backend(backend: str | None) -> str:
    key = (backend or "ollama").strip().lower()
    if key in ("ollama", ""):
        return "ollama"
    if key == "openai":
        return "openai"
    raise LLMGenerationError(f"Unsupported LLM backend '{backend}'")


def _is_levelled_ollama_model(model: str) -> bool:
    return any(model.strip().lower().startswith(prefix) for prefix in _OLLAMA_LEVELLED_THINKING)


def _build_llm_caller(
    *,
    backend: str,
    default_model: str,
    endpoint: str,
    openai_model: str | None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    num_ctx: int = DEFAULT_NUM_CTX,
) -> LLMCaller:
    """Return a callable that executes requests against the requested LLM backend."""

    backend_key = _normalize_backend(backend)
    if backend_key == "openai":
        model_name = (openai_model or DEFAULT_OPENAI_MODEL).strip()
        offered = [model_id for model_id, _ in OPENAI_MODELS]
        if model_name not in offered:
            raise LLMGenerationError(
                f"OpenAI model '{model_name}' is not supported. Choose one of: {', '.join(offered)}"
            )
        effort = resolve_reasoning_effort(backend_key, model_name, reasoning_effort)
        client = _get_openai_client()
        logger.info("Using OpenAI model %s, thinking level %s", model_name, effort)

        def _call(llm_request: LLMRequest) -> str:
            return _timed(
                llm_request,
                lambda: _call_openai(model_name, llm_request, effort=effort, client=client),
            )

        return _call

    model_name = (default_model or DEFAULT_OLLAMA_MODEL).strip()
    think = _ollama_think_option(endpoint, model_name, reasoning_effort)
    use_schema = not model_name.lower().startswith(_OLLAMA_SCHEMA_UNSUPPORTED)
    logger.info(
        "Using Ollama model %s, thinking %s",
        model_name,
        "not supported" if think is None else think,
    )

    def _call(llm_request: LLMRequest) -> str:
        context = _ollama_num_ctx(llm_request.prompt, num_ctx)
        return _timed(
            llm_request,
            lambda: _call_ollama(
                endpoint,
                model_name,
                llm_request,
                think=think,
                num_ctx=context,
                use_schema=use_schema,
            ),
        )

    return _call


def _timed(llm_request: LLMRequest, call: Callable[[], str]) -> str:
    started = time.monotonic()
    try:
        return call()
    finally:
        logger.info(
            "LLM call '%s' took %.1fs", llm_request.schema_name, time.monotonic() - started
        )


def _ask(
    call_llm: LLMCaller,
    llm_request: LLMRequest,
    parse: Callable[[str], Any],
    *,
    on_retry: Callable[[], None],
) -> Any:
    """Call the LLM and parse its answer, re-asking once if the answer is rejected.

    The retry carries the rejection reason, so the model can correct the one
    mistake instead of the whole generation starting over.
    """

    response_text = call_llm(llm_request)
    try:
        return parse(response_text)
    except LLMGenerationError as exc:
        logger.warning("Answer for '%s' rejected, retrying once: %s", llm_request.schema_name, exc)
        on_retry()
        retry = LLMRequest(
            prompt=(
                f"{llm_request.prompt}\n\n"
                f"Your previous answer was rejected: {exc}\n"
                "Answer again, correcting that."
            ),
            schema=llm_request.schema,
            schema_name=llm_request.schema_name,
        )
        return parse(call_llm(retry))


def _compose_prompt(user_prompt: str, catalog: ModelCatalog) -> str:
    """The block-selection prompt. Everything static comes first and the user's
    goal last, so both Ollama and OpenAI can reuse the cached prompt prefix."""

    instructions = (
        "You are a tone designer building signal chains for the Line 6 Helix. "
        "Choose the blocks that best achieve the user's goal and give the chain a short title.\n"
        "Rules:\n"
        f"- Use between 1 and {_MAX_BLOCKS} blocks, each named exactly as in the model list below.\n"
        "- Select only blocks that directly support the requested tone. A category can be "
        "skipped when it doesn't fit; for example, a distortion is not always needed.\n"
        "- The chain must include an amp, a cab, and a reverb.\n"
        "- Order: dynamics -> drive/distortion -> modulation -> amp -> cab -> delay -> reverb.\n"
        "- Choose blocks only. Parameters are set in a separate step.\n"
        'Answer with one JSON object: {"title": string, "blocks": [model names in order]}.'
    )
    examples = "\n".join(
        f"Example {index} - goal: {sample['goal']}\n"
        f"{json.dumps(sample['response'], separators=(',', ':'))}"
        for index, sample in enumerate(_FEWSHOT_EXAMPLES, start=1)
    )
    return "\n\n".join(
        [
            instructions,
            examples,
            "Available models by category (name - real-world reference):\n"
            + _summarize_catalog(catalog),
            f"User goal: {user_prompt.strip()}",
        ]
    )


def _summarize_catalog(catalog: ModelCatalog) -> str:
    """One line per model, grouped under category headers, in a stable order."""

    grouped: dict[str, list[str]] = {}
    for model in sorted(catalog.models(), key=lambda m: m.display_name.lower()):
        line = model.display_name
        if model.based_on:
            line += f" - {model.based_on}"
        grouped.setdefault(model.category or "Uncategorized", []).append(line)
    return "\n".join(
        f"## {category}\n" + "\n".join(lines) for category, lines in grouped.items()
    )


def _chain_schema(catalog: ModelCatalog) -> dict[str, Any]:
    """Block selection answer schema. Only catalog names are admissible, so a
    constrained decoder cannot invent a model."""

    names = sorted({model.display_name for model in catalog.models()}, key=str.lower)
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "blocks": {
                "type": "array",
                "items": {"type": "string", "enum": names},
                "minItems": 1,
                "maxItems": _MAX_BLOCKS,
            },
        },
        "required": ["title", "blocks"],
        "additionalProperties": False,
    }


def _parse_chain_response(
    response_text: str, catalog: ModelCatalog
) -> tuple[dict[str, Any], list[ModelDefinition]]:
    spec = _parse_json_response(response_text)
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
    return chain, resolved_models


def _ollama_url(endpoint: str, path: str) -> str:
    """Another Ollama API path on the same server as the generate ``endpoint``."""

    base = endpoint.split("/api/", 1)[0] if "/api/" in endpoint else endpoint.rstrip("/")
    return base + path


def _ollama_think_option(endpoint: str, model_name: str, reasoning_effort: str) -> str | bool | None:
    """Map a thinking level onto Ollama's ``think`` option, or None to omit it."""

    if not _ollama_supports_thinking(endpoint, model_name):
        logger.info("Ollama model %s does not think; thinking level ignored", model_name)
        return None
    effort = resolve_reasoning_effort("ollama", model_name, reasoning_effort)
    if effort != reasoning_effort:
        logger.info(
            "Ollama model %s has no '%s' thinking level; using '%s'",
            model_name,
            reasoning_effort,
            effort,
        )
    if _is_levelled_ollama_model(model_name):
        return effort
    return effort != "none"


@lru_cache(maxsize=32)
def _ollama_supports_thinking(endpoint: str, model_name: str) -> bool:
    try:
        body = _ollama_request(
            _ollama_url(endpoint, "/api/show"), {"model": model_name}, timeout=10
        )
    except LLMGenerationError as exc:
        # Older servers have no capability list; fall back to what we know.
        logger.info("Could not read capabilities of %s (%s)", model_name, exc)
        return _is_levelled_ollama_model(model_name)
    capabilities = body.get("capabilities") if isinstance(body, dict) else None
    if not isinstance(capabilities, list):
        return _is_levelled_ollama_model(model_name)
    return "thinking" in capabilities


def _ollama_num_ctx(prompt: str, requested: int) -> int:
    """``requested``, raised if the prompt plus room for an answer would not fit.

    Estimates at three characters per token, which overestimates for the
    English and JSON these prompts contain, so the estimate errs on the safe side.
    """

    needed = len(prompt) // 3 + _OUTPUT_HEADROOM_TOKENS
    if needed <= requested:
        return requested
    raised = -(-needed // 4096) * 4096
    logger.info("Raising Ollama context window from %d to %d tokens to fit the prompt", requested, raised)
    return raised


def _ollama_request(url: str, payload: dict[str, Any] | None, *, timeout: float) -> Any:
    """POST ``payload`` (or GET when None) to Ollama and return the decoded JSON."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
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
        raise LLMGenerationError(f"Failed to reach Ollama endpoint {url}: {exc}") from exc
    except TimeoutError as exc:
        raise LLMGenerationError(f"Ollama did not answer within {timeout:.0f}s") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMGenerationError("Ollama returned invalid JSON") from exc


def _call_ollama(
    endpoint: str,
    model_name: str,
    llm_request: LLMRequest,
    *,
    think: str | bool | None = None,
    num_ctx: int = DEFAULT_NUM_CTX,
    use_schema: bool = True,
) -> str:
    payload: dict[str, Any] = {
        "model": model_name,
        "prompt": llm_request.prompt,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"num_ctx": num_ctx},
    }
    if use_schema:
        payload["format"] = llm_request.schema
    if think is not None:
        payload["think"] = think
    parsed = _ollama_request(endpoint, payload, timeout=OLLAMA_TIMEOUT_SECONDS)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise LLMGenerationError(str(parsed["error"]))
    if not isinstance(parsed, dict) or "response" not in parsed:
        raise LLMGenerationError("Unexpected response from Ollama")
    _log_ollama_metrics(llm_request.schema_name, parsed, num_ctx)
    response_text = parsed.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        raise LLMGenerationError("Ollama response did not contain text output")
    return response_text


def _log_ollama_metrics(name: str, body: dict[str, Any], num_ctx: int) -> None:
    def seconds(key: str) -> float:
        value = body.get(key)
        return value / 1e9 if isinstance(value, (int, float)) else 0.0

    prompt_tokens = body.get("prompt_eval_count")
    logger.info(
        "Ollama '%s': load %.1fs, prompt %s tokens in %.1fs, output %s tokens in %.1fs",
        name,
        seconds("load_duration"),
        prompt_tokens,
        seconds("prompt_eval_duration"),
        body.get("eval_count"),
        seconds("eval_duration"),
    )
    if isinstance(prompt_tokens, int) and prompt_tokens >= num_ctx * 0.9:
        logger.warning(
            "Ollama '%s' prompt used %d of %d context tokens; it may have been truncated",
            name,
            prompt_tokens,
            num_ctx,
        )


def _call_openai(
    model_name: str,
    llm_request: LLMRequest,
    *,
    effort: str = DEFAULT_REASONING_EFFORT,
    client: Any | None = None,
) -> str:
    client = client or _get_openai_client()
    try:
        response = client.responses.create(
            model=model_name,
            input=llm_request.prompt,
            reasoning={"effort": effort},
            text={
                "format": {
                    "type": "json_schema",
                    "name": llm_request.schema_name,
                    "schema": llm_request.schema,
                    "strict": True,
                }
            },
        )
    except Exception as exc:  # pragma: no cover - network errors not hit in tests
        raise LLMGenerationError(f"OpenAI request failed: {exc}") from exc
    _log_openai_usage(llm_request.schema_name, response)
    text = _extract_text_from_openai_response(response)
    if not text:
        raise LLMGenerationError("OpenAI response did not contain text output")
    return text


def _log_openai_usage(name: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    logger.info(
        "OpenAI '%s': input %s tokens (%s cached), output %s tokens (%s reasoning)",
        name,
        getattr(usage, "input_tokens", None),
        getattr(input_details, "cached_tokens", None),
        getattr(usage, "output_tokens", None),
        getattr(output_details, "reasoning_tokens", None),
    )


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
    call_llm: LLMCaller,
    chain: dict[str, Any],
    models: list[ModelDefinition],
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Set every block's parameters with a single LLM call."""

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    chain_title = chain.get("meta", {}).get("name") or chain.get("title") or "Generated Tone"
    ordered_block_names = [model.display_name for model in models]

    # Blocks keyed by position ("block1", ...) rather than name: a chain may hold
    # the same model twice, and each one gets its own settings.
    targets: dict[str, tuple[dict[str, Any], ModelDefinition, list[dict[str, Any]]]] = {}
    for index, (block_spec, model) in enumerate(zip(chain["blocks"], models, strict=True)):
        if "cab" in (model.category or "").lower():
            logger.info(
                "Skipping parameter selection for cab block '%s'; using defaults.",
                model.display_name,
            )
            _report(f"Skipping '{model.display_name}' (cab defaults used).")
            continue
        parameter_summary = _summarize_parameters(model)
        if not parameter_summary:
            logger.info("No adjustable parameters found for block '%s'", model.display_name)
            _report(f"No adjustable parameters for '{model.display_name}'.")
            continue
        targets[f"block{index + 1}"] = (block_spec, model, parameter_summary)

    if not targets:
        return

    logger.info("Selecting parameters for %d block(s)", len(targets))
    _report(f"Setting parameters for {len(targets)} block(s)...")
    started = time.monotonic()
    parameters_request = LLMRequest(
        prompt=_compose_parameter_prompt(
            user_prompt=prompt,
            chain_title=chain_title,
            chain_blocks=ordered_block_names,
            targets={key: (model, summary) for key, (_, model, summary) in targets.items()},
        ),
        schema=_parameters_schema(
            {key: summary for key, (_, _, summary) in targets.items()}
        ),
        schema_name="parameters",
    )
    chosen = _ask(
        call_llm,
        parameters_request,
        lambda text: _parse_parameters_response(
            text, {key: model for key, (_, model, _) in targets.items()}
        ),
        on_retry=lambda: _report("Retrying parameter selection..."),
    )

    for key, (block_spec, model, _) in targets.items():
        normalized_parameters = chosen.get(key)
        if normalized_parameters is None:
            logger.warning(
                "LLM returned no settings for '%s'; defaults will be used.", model.display_name
            )
            _report(f"Using defaults for '{model.display_name}'.")
        elif normalized_parameters:
            block_spec.setdefault("parameters", {}).update(normalized_parameters)
            logger.info(
                "Parameters selected for '%s': %s", model.display_name, normalized_parameters
            )
        else:
            logger.info(
                "LLM kept every default for '%s'.", model.display_name
            )

    elapsed = time.monotonic() - started
    _report(f"Parameters set for {len(targets)} block(s) ({elapsed:.1f}s)")


def _compose_parameter_prompt(
    user_prompt: str,
    chain_title: str,
    chain_blocks: Iterable[str],
    targets: dict[str, tuple[ModelDefinition, list[dict[str, Any]]]],
) -> str:
    instructions = (
        "You are configuring the parameters of the blocks in a Line 6 Helix preset. "
        "Use the player's request and the whole chain to choose musical values that "
        "work together; for example, stage gain across drives and the amp.\n"
        "Rules:\n"
        "- Stay within each parameter's range. For options, use a label exactly as listed.\n"
        "- Keep gain and drive moderate on distortion pedals, compressors, and amps: "
        "drives are used together with the amp to reach high gain.\n"
        "- Use null for a parameter that should stay at its default.\n"
        'Answer with one JSON object: {"blocks": {"block1": {"<parameter>": value, ...}, ...}} '
        "covering every block listed below."
    )
    blocks_text = "\n\n".join(
        _render_block(key, model, summary) for key, (model, summary) in targets.items()
    )
    return (
        f"{instructions}\n\n"
        f"Player request: {user_prompt.strip()}\n"
        f"Chain title: {chain_title}\n"
        f"Full chain order: {' -> '.join(chain_blocks)}\n\n"
        f"Blocks to configure:\n\n{blocks_text}"
    )


def _render_block(key: str, model: ModelDefinition, summary: list[dict[str, Any]]) -> str:
    header = f"{key}: {model.display_name} ({model.category or 'Uncategorized'}"
    if model.based_on:
        header += f", based on {model.based_on}"
    lines = [header + ")"]
    for entry in summary:
        line = f"- {entry['name']}: {entry['type']}"
        if entry["type"] in ("continuous", "number") and entry.get("min") is not None:
            line += f" {_format_number(entry.get('min'))}..{_format_number(entry.get('max'))}"
        elif entry["type"] == "enum":
            line += " [" + ", ".join(entry.get("options", [])) + "]"
        if "default" in entry:
            line += f", default {_format_default(entry)}"
        lines.append(line)
    return "\n".join(lines)


def _format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_default(entry: dict[str, Any]) -> str:
    default = entry["default"]
    if entry["type"] == "enum":
        labels = entry.get("option_values", {})
        return str(labels.get(default, default))
    if entry["type"] == "boolean":
        return "true" if default else "false"
    return _format_number(default)


def _summarize_parameters(model: ModelDefinition) -> list[dict[str, Any]]:
    """The adjustable (non-@) parameters of ``model`` with their types and limits."""

    summary: list[dict[str, Any]] = []
    for name in sorted(model.parameter_names()):
        if name.startswith("@"):
            continue
        definition = model.get_parameter(name)

        entry: dict[str, Any] = {"name": name}
        default = definition.default_value()
        if default is not None:
            entry["default"] = default

        if definition.value_type == CONTINUOUS:
            entry["type"] = "continuous"
            if definition.min_value is not None:
                entry["min"] = definition.min_value
            if definition.max_value is not None:
                entry["max"] = definition.max_value
        elif definition.display_type == "boolean":
            entry["type"] = "boolean"
        elif definition.forward_map:
            entry["type"] = "enum"
            ordered = sorted(definition.forward_map.items(), key=lambda item: _enum_sort_key(item[0]))
            options: list[str] = []
            option_values: dict[Any, str] = {}
            for raw_value, label in ordered:
                label_text = str(label)
                if label_text not in options:
                    options.append(label_text)
                option_values[_enum_value(raw_value)] = label_text
            entry["options"] = options
            entry["option_values"] = option_values
        else:
            # A stepped parameter the dataset has no labels for. It takes whole
            # option numbers, so its range is what the model has to go on.
            entry["type"] = "number"
            if definition.min_value is not None:
                entry["min"] = definition.min_value
            if definition.max_value is not None:
                entry["max"] = definition.max_value

        summary.append(entry)
    return summary


def _enum_value(raw_value: str) -> Any:
    try:
        return int(raw_value)
    except ValueError:
        return raw_value


def _enum_sort_key(raw_value: str) -> tuple[int, Any]:
    value = _enum_value(raw_value)
    return (0, value) if isinstance(value, int) else (1, value)


def _parameters_schema(targets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Parameter answer schema: one object per block, every parameter nullable
    (null keeps the default), limits and option labels enforced."""

    blocks: dict[str, Any] = {}
    for key, summary in targets.items():
        properties = {entry["name"]: _parameter_schema(entry) for entry in summary}
        blocks[key] = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "object",
                "properties": blocks,
                "required": list(blocks),
                "additionalProperties": False,
            }
        },
        "required": ["blocks"],
        "additionalProperties": False,
    }


def _parameter_schema(entry: dict[str, Any]) -> dict[str, Any]:
    kind = entry["type"]
    if kind == "continuous":
        schema: dict[str, Any] = {"type": ["number", "null"]}
        if entry.get("min") is not None:
            schema["minimum"] = entry["min"]
        if entry.get("max") is not None:
            schema["maximum"] = entry["max"]
        return schema
    if kind == "boolean":
        return {"type": ["boolean", "null"]}
    if kind == "enum":
        return {"type": ["string", "null"], "enum": [*entry["options"], None]}
    schema = {"type": ["integer", "null"]}
    if entry.get("min") is not None:
        schema["minimum"] = entry["min"]
    if entry.get("max") is not None:
        schema["maximum"] = entry["max"]
    return schema


def _parse_parameters_response(
    response_text: str, models: dict[str, ModelDefinition]
) -> dict[str, dict[str, Any]]:
    """Map each block key to its normalized parameters, nulls dropped.

    Blocks the answer leaves out are absent from the result (their defaults stay).
    Raises :class:`LLMGenerationError` for anything that cannot be applied.
    """

    parsed = _parse_json_response(response_text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("blocks"), dict):
        raise LLMGenerationError(
            "LLM parameter response must be a JSON object with a 'blocks' object."
        )
    blocks = parsed["blocks"]
    result: dict[str, dict[str, Any]] = {}
    for key, model in models.items():
        raw = blocks.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise LLMGenerationError(
                f"Settings for {key} ('{model.display_name}') must be a JSON object."
            )
        chosen = {name: value for name, value in raw.items() if value is not None}
        result[key] = _normalize_parameters(model, chosen)
    return result


def _normalize_parameters(
    model: ModelDefinition,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, value in parameters.items():
        if name not in model.parameters or name.startswith("@"):
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


def _design_snapshots_with_llm(
    prompt: str,
    call_llm: LLMCaller,
    chain: dict[str, Any],
    models: list[ModelDefinition],
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Design the preset's snapshots with a single LLM call.

    Without this a preset is one fixed sound. Each snapshot switches blocks on or
    off and re-dials the parameters it needs, so one preset covers several usable
    tones -- clean, rhythm, lead -- at the tap of a footswitch.
    """

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    chain_title = chain.get("meta", {}).get("name") or chain.get("title") or "Generated Tone"

    # Keyed by position, as the parameter round is: a chain may hold the same
    # model twice and each copy is switched and dialled on its own.
    targets: dict[str, tuple[ModelDefinition, list[dict[str, Any]]]] = {}
    for index, (block_spec, model) in enumerate(zip(chain["blocks"], models, strict=True)):
        chosen = block_spec.get("parameters", {}) if isinstance(block_spec, dict) else {}
        summary = []
        if "cab" not in (model.category or "").lower():
            for entry in _summarize_parameters(model):
                definition = model.get_parameter(entry["name"])
                if definition.controller_range() is None:
                    # Nothing to sweep: the device cannot put it in a snapshot.
                    continue
                entry["current"] = _current_value(definition, chosen.get(entry["name"]))
                summary.append(entry)
        targets[f"block{index + 1}"] = (model, summary)

    logger.info("Designing %d snapshots", SNAPSHOT_COUNT)
    _report(f"Designing {SNAPSHOT_COUNT} snapshots...")
    started = time.monotonic()
    snapshots_request = LLMRequest(
        prompt=_compose_snapshot_prompt(
            user_prompt=prompt, chain_title=chain_title, targets=targets
        ),
        schema=_snapshots_schema({key: summary for key, (_, summary) in targets.items()}),
        schema_name="snapshots",
    )
    chain["snapshots"] = _ask(
        call_llm,
        snapshots_request,
        lambda text: _parse_snapshots_response(
            text, {key: model for key, (model, _) in targets.items()}
        ),
        on_retry=lambda: _report("Retrying snapshot design..."),
    )

    elapsed = time.monotonic() - started
    names = ", ".join(snapshot["name"] for snapshot in chain["snapshots"])
    logger.info("Snapshots designed in %.1fs: %s", elapsed, names)
    _report(f"Snapshots: {names} ({elapsed:.1f}s)")


def _current_value(definition: ParameterDefinition, chosen: Any) -> Any:
    """What the block is set to now, written the way the answer must write it."""
    value = definition.default_value() if chosen is None else chosen
    if definition.display_type == "boolean" or isinstance(value, bool):
        return bool(value)
    if definition.forward_map and value is not None:
        return definition.forward_map.get(str(value), value)
    return value


def _compose_snapshot_prompt(
    user_prompt: str,
    chain_title: str,
    targets: dict[str, tuple[ModelDefinition, list[dict[str, Any]]]],
) -> str:
    instructions = (
        f"You are designing the {SNAPSHOT_COUNT} snapshots of a Line 6 HX Stomp preset. "
        "A snapshot recalls which blocks are on or off and the value of every parameter "
        "listed below, so one preset gives the player several usable sounds at the tap of "
        "a footswitch.\n"
        "Rules:\n"
        f"- Give exactly {SNAPSHOT_COUNT} snapshots, clearly different from one another but "
        "all fitting the player's request. Pick the roles that suit it, for example "
        "clean / crunch / high gain, rhythm / solo / ambient, or clean / rhythm / lead.\n"
        f"- Name each one in at most {SNAPSHOT_NAME_LIMIT} characters.\n"
        "- Every block starts on, at the value shown as 'now'. Use null for anything that "
        "should stay as it is; only write what the snapshot changes.\n"
        "- A clean snapshot switches drives off and lowers amp drive; a lead or high gain "
        "one raises drive or engages a drive pedal; a solo lifts output level a little and "
        "adds delay; an ambient one raises delay and reverb mix, feedback and decay.\n"
        "- When you lower gain, raise the amp's channel volume a little, so the snapshots "
        "stay even in loudness.\n"
        "- Keep amp and cab blocks on in every snapshot.\n"
        '- Answer with one JSON object: {"snapshots": [{"name": "Clean", "blocks": '
        '{"block1": {"enabled": false, "parameters": {"<parameter>": value, ...}}, ...}}, ...]}'
    )
    blocks_text = "\n\n".join(
        _render_snapshot_block(key, model, summary) for key, (model, summary) in targets.items()
    )
    return (
        f"{instructions}\n\n"
        f"Player request: {user_prompt.strip()}\n"
        f"Chain title: {chain_title}\n\n"
        f"Blocks, in signal order:\n\n{blocks_text}"
    )


def _render_snapshot_block(
    key: str, model: ModelDefinition, summary: list[dict[str, Any]]
) -> str:
    header = f"{key}: {model.display_name} ({model.category or 'Uncategorized'})"
    if not summary:
        return f"{header}\n- on/off only"
    lines = [header]
    for entry in summary:
        line = f"- {entry['name']}: {entry['type']}"
        if entry["type"] in ("continuous", "number") and entry.get("min") is not None:
            line += f" {_format_number(entry.get('min'))}..{_format_number(entry.get('max'))}"
        elif entry["type"] == "enum":
            line += " [" + ", ".join(entry.get("options", [])) + "]"
        line += f", now {entry['current']}"
        lines.append(line)
    return "\n".join(lines)


def _snapshots_schema(targets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Snapshot answer schema: a fixed number of named snapshots, each listing
    every block, with the same nullable parameter schema the parameter round uses."""

    blocks: dict[str, Any] = {}
    for key, summary in targets.items():
        properties: dict[str, Any] = {"enabled": {"type": ["boolean", "null"]}}
        if summary:
            parameters = {entry["name"]: _parameter_schema(entry) for entry in summary}
            properties["parameters"] = {
                "type": ["object", "null"],
                "properties": parameters,
                "required": list(parameters),
                "additionalProperties": False,
            }
        blocks[key] = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
    snapshot = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": SNAPSHOT_NAME_LIMIT},
            "blocks": {
                "type": "object",
                "properties": blocks,
                "required": list(blocks),
                "additionalProperties": False,
            },
        },
        "required": ["name", "blocks"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "snapshots": {
                "type": "array",
                "items": snapshot,
                "minItems": SNAPSHOT_COUNT,
                "maxItems": SNAPSHOT_COUNT,
            }
        },
        "required": ["snapshots"],
        "additionalProperties": False,
    }


def _parse_snapshots_response(
    response_text: str, models: dict[str, ModelDefinition]
) -> list[dict[str, Any]]:
    """Turn the answer into the chain's ``snapshots`` list.

    Block keys are one-based in the prompt, as they are in the parameter round,
    and zero-based indexes in a chain; nulls and blocks a snapshot does not change
    are dropped, leaving each block's own settings to stand.
    """

    parsed = _parse_json_response(response_text)
    snapshots = parsed.get("snapshots") if isinstance(parsed, dict) else None
    if not isinstance(snapshots, list) or len(snapshots) != SNAPSHOT_COUNT:
        raise LLMGenerationError(
            f"LLM snapshot response must be a JSON object with a 'snapshots' list of "
            f"exactly {SNAPSHOT_COUNT} entries."
        )

    result: list[dict[str, Any]] = []
    for number, snapshot in enumerate(snapshots, start=1):
        if not isinstance(snapshot, dict):
            raise LLMGenerationError(f"Snapshot {number} must be a JSON object.")
        name = snapshot.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LLMGenerationError(f"Snapshot {number} must have a non-empty 'name'.")
        blocks = snapshot.get("blocks") or {}
        if not isinstance(blocks, dict):
            raise LLMGenerationError(
                f"Snapshot '{name}' must map block keys to their settings."
            )

        changes: dict[str, Any] = {}
        for key, settings in blocks.items():
            model = models.get(key)
            if model is None:
                raise LLMGenerationError(f"Snapshot '{name}' names unknown block '{key}'.")
            if settings is None:
                continue
            if not isinstance(settings, dict):
                raise LLMGenerationError(
                    f"Snapshot '{name}' settings for '{model.display_name}' must be an object."
                )
            entry: dict[str, Any] = {}
            enabled = settings.get("enabled")
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise LLMGenerationError(
                        f"Snapshot '{name}' 'enabled' for '{model.display_name}' must be "
                        "true or false."
                    )
                entry["enabled"] = enabled
            chosen = {
                parameter: value
                for parameter, value in (settings.get("parameters") or {}).items()
                if value is not None
            }
            if chosen:
                entry["parameters"] = _normalize_parameters(model, chosen)
            if entry:
                changes[str(int(key.removeprefix("block")) - 1)] = entry
        result.append({"name": name.strip(), "blocks": changes})
    return result
