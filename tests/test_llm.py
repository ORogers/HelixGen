import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from helixgen.dataset import ModelCatalog
from helixgen.llm import (
    _FEWSHOT_EXAMPLES,
    DEFAULT_OPENAI_MODEL,
    OPENAI_MODELS,
    REASONING_EFFORTS,
    SNAPSHOT_COUNT,
    SNAPSHOT_NAME_LIMIT,
    LLMGenerationError,
    LLMRequest,
    _build_llm_caller,
    _chain_schema,
    _compose_prompt,
    _ollama_num_ctx,
    _parameters_schema,
    _snapshots_schema,
    _summarize_parameters,
    generate_chain_from_prompt,
    list_llm_models,
    resolve_reasoning_effort,
    supported_reasoning_efforts,
)


def _default_snapshots() -> dict[str, Any]:
    return {
        "snapshots": [
            {"name": f"Snap {number}", "blocks": {}}
            for number in range(1, SNAPSHOT_COUNT + 1)
        ]
    }


def _install_fake_ollama(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chain: Any,
    parameters: Any = None,
    snapshots: Any = None,
) -> list[tuple[LLMRequest, dict[str, Any]]]:
    """Answer each round with the given object (or raw string) and record the calls."""

    calls: list[tuple[LLMRequest, dict[str, Any]]] = []
    answers = {"chain": chain, "parameters": parameters, "snapshots": snapshots}
    fallbacks = {"parameters": {"blocks": {}}, "snapshots": _default_snapshots()}

    def fake_call(endpoint: str, model_name: str, llm_request: LLMRequest, **options: Any) -> str:
        _ = endpoint, model_name
        calls.append((llm_request, options))
        answer = answers.get(llm_request.schema_name)
        if callable(answer):
            answer = answer(llm_request)
        if answer is None:
            answer = fallbacks.get(llm_request.schema_name, {"blocks": {}})
        return answer if isinstance(answer, str) else json.dumps(answer)

    monkeypatch.setattr("helixgen.llm._call_ollama", fake_call)
    return calls


def test_compose_prompt_lists_every_catalog_model(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Clean tone", catalog)

    assert "Available models by category" in prompt
    for model in catalog.models():
        assert model.display_name in prompt
    assert "Horizon Drive" in prompt
    # Internal IDs never help the model choose, so they stay out of the prompt.
    assert "HD2_" not in prompt


def test_compose_prompt_keeps_the_user_goal_last(dataset_path):
    """The static prefix must come first so both backends can reuse cached prefixes."""
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Ambient wash", catalog)

    assert prompt.rstrip().endswith("User goal: Ambient wash")
    assert _compose_prompt("Other", catalog).split("User goal:")[0] == prompt.split("User goal:")[0]


def test_compose_prompt_is_compact(dataset_path):
    catalog = ModelCatalog(dataset_path)
    # The previous prompt was ~48k characters of indented JSON.
    assert len(_compose_prompt("Clean tone", catalog)) < 20000


def test_compose_prompt_includes_fewshot_examples(dataset_path):
    catalog = ModelCatalog(dataset_path)
    prompt = _compose_prompt("Ambient", catalog)

    for index, sample in enumerate(_FEWSHOT_EXAMPLES, start=1):
        assert f"Example {index} - goal: {sample['goal']}" in prompt
        assert json.dumps(sample["response"], separators=(",", ":")) in prompt


def test_fewshot_examples_use_real_catalog_models(dataset_path):
    catalog = ModelCatalog(dataset_path)

    for sample in _FEWSHOT_EXAMPLES:
        blocks = sample["response"]["blocks"]
        assert blocks, f"few-shot example {sample['goal']!r} has no blocks"
        for name in blocks:
            assert catalog.has_model(name), (
                f"few-shot example {sample['goal']!r} references "
                f"unknown model {name!r}"
            )


def test_chain_schema_only_admits_catalog_names(dataset_path):
    catalog = ModelCatalog(dataset_path)
    schema = _chain_schema(catalog)

    names = schema["properties"]["blocks"]["items"]["enum"]
    assert set(names) == {model.display_name for model in catalog.models()}
    assert schema["required"] == ["title", "blocks"]
    assert schema["additionalProperties"] is False


def test_parameters_schema_is_strict_and_nullable(dataset_path):
    catalog = ModelCatalog(dataset_path)
    summary = _summarize_parameters(catalog.get("Deluxe Comp"))
    schema = _parameters_schema({"block1": summary})

    block = schema["properties"]["blocks"]["properties"]["block1"]
    assert block["additionalProperties"] is False
    assert set(block["required"]) == {entry["name"] for entry in summary}
    # Options are offered by label, and null keeps the default.
    assert block["properties"]["Ratio"]["enum"][:2] == ["2:1", "3:1"]
    assert None in block["properties"]["Ratio"]["enum"]
    assert "null" in block["properties"]["Mix"]["type"]


def test_generate_chain_requires_object(monkeypatch: pytest.MonkeyPatch, dataset_path: Path):
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(
        monkeypatch,
        chain=[{"internal_name": "HD2_DistClean", "params": {"Clean": 1.0}}],
    )

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(prompt="clean tone", catalog=catalog, llm_model="fake-model")

    assert "JSON object" in str(excinfo.value)


def test_generate_chain_converts_minimal_spec(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(
        monkeypatch, chain={"title": "Clean Tone", "blocks": ["Horizon Drive", "Transistor Tape"]}
    )

    chain = generate_chain_from_prompt(prompt="clean tone", catalog=catalog, llm_model="fake-model")

    assert chain["meta"]["name"] == "Clean Tone"
    assert chain["blocks"] == [
        {"model": "Horizon Drive"},
        {"model": "Transistor Tape"},
    ]


def test_generate_chain_makes_three_calls_for_any_chain_length(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    blocks = ["Deluxe Comp", "Scream 808", "Brit Plexi Nrm", "4x12 Greenback 25", "Plate Reverb"]
    calls = _install_fake_ollama(monkeypatch, chain={"title": "Five", "blocks": blocks})

    generate_chain_from_prompt(prompt="five blocks", catalog=catalog, llm_model="fake-model")

    assert [request.schema_name for request, _ in calls] == ["chain", "parameters", "snapshots"]
    parameters_request = calls[1][0]
    configured = parameters_request.schema["properties"]["blocks"]["properties"]
    # Every block but the cab (block4) is configured in the one call.
    assert list(configured) == ["block1", "block2", "block3", "block5"]
    assert "block1: Deluxe Comp" in parameters_request.prompt


def test_generate_chain_applies_parameters_by_position(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(
        monkeypatch,
        chain={"title": "Twin Drives", "blocks": ["Scream 808", "Scream 808", "Deluxe Comp"]},
        parameters={
            "blocks": {
                "block1": {"Gain": 0.3, "Tone": None},
                "block2": {"Gain": 0.7},
                "block3": {"Ratio": "4:1"},
            }
        },
    )

    chain = generate_chain_from_prompt(prompt="two drives", catalog=catalog, llm_model="fake")

    assert chain["blocks"][0]["parameters"] == {"Gain": 0.3}
    assert chain["blocks"][1]["parameters"] == {"Gain": 0.7}
    # Option labels are stored as their option numbers.
    assert chain["blocks"][2]["parameters"] == {"Ratio": 2}


def test_generate_chain_keeps_defaults_for_blocks_the_answer_omits(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(
        monkeypatch,
        chain={"title": "Partial", "blocks": ["Scream 808", "Plate Reverb"]},
        parameters={"blocks": {"block1": {"Gain": 0.4}}},
    )

    chain = generate_chain_from_prompt(prompt="partial", catalog=catalog, llm_model="fake")

    assert chain["blocks"][0]["parameters"] == {"Gain": 0.4}
    assert "parameters" not in chain["blocks"][1]


def test_generate_chain_includes_optional_metadata(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(
        monkeypatch,
        chain={
            "title": "Sparkly",
            "author": "Tone Bot",
            "description": "Bright and chimy",
            "blocks": ["US Double Nrm"],
        },
    )

    chain = generate_chain_from_prompt(prompt="sparkly clean", catalog=catalog, llm_model="fake")

    assert chain["meta"] == {
        "name": "Sparkly",
        "author": "Tone Bot",
        "description": "Bright and chimy",
    }


def test_generate_chain_rejects_non_string_block(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    _install_fake_ollama(monkeypatch, chain={"title": "Bad", "blocks": [123]})

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(prompt="bad", catalog=catalog, llm_model="fake-model")

    assert "block entry" in str(excinfo.value).lower()


def test_generate_chain_rejects_unknown_model_after_one_retry(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    calls = _install_fake_ollama(
        monkeypatch, chain={"title": "Mystery", "blocks": ["NotARealModel"]}
    )

    with pytest.raises(LLMGenerationError) as excinfo:
        generate_chain_from_prompt(prompt="mystery", catalog=catalog, llm_model="fake-model")

    assert "unknown model" in str(excinfo.value).lower()
    assert [request.schema_name for request, _ in calls] == ["chain", "chain"]
    assert "Your previous answer was rejected: Unknown model 'NotARealModel'" in calls[1][0].prompt


def test_generate_chain_recovers_when_the_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def parameters(llm_request: LLMRequest) -> dict[str, Any]:
        if "previous answer was rejected" in llm_request.prompt:
            return {"blocks": {"block1": {"Gain": 0.5}}}
        return {"blocks": {"block1": {"NotAParameter": 1.0}}}

    calls = _install_fake_ollama(
        monkeypatch, chain={"title": "Retry", "blocks": ["Scream 808"]}, parameters=parameters
    )
    messages: list[str] = []

    chain = generate_chain_from_prompt(
        prompt="retry", catalog=catalog, llm_model="fake", on_progress=messages.append
    )

    assert chain["blocks"][0]["parameters"] == {"Gain": 0.5}
    assert [request.schema_name for request, _ in calls] == [
        "chain", "parameters", "parameters", "snapshots",
    ]
    assert "Retrying parameter selection..." in messages


def test_cab_parameters_use_defaults(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)

    def parameters(llm_request: LLMRequest) -> str:
        pytest.fail("Cab blocks should not trigger parameter selection.")

    _install_fake_ollama(
        monkeypatch, chain={"title": "Cab Only", "blocks": ["2x12 Blue Bell"]}, parameters=parameters
    )

    chain = generate_chain_from_prompt(prompt="just a cab", catalog=catalog, llm_model="fake")

    assert chain["blocks"] == [{"model": "2x12 Blue Bell"}]
    assert "parameters" not in chain["blocks"][0]


def test_ollama_calls_carry_thinking_and_context_options(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    calls = _install_fake_ollama(monkeypatch, chain={"title": "T", "blocks": ["Scream 808"]})

    generate_chain_from_prompt(
        prompt="t",
        catalog=catalog,
        llm_model="gpt-oss:20b",
        reasoning_effort="max",
        num_ctx=16384,
    )

    # gpt-oss tops out at "high"; every call uses the same context window so
    # Ollama never reloads the model between rounds.
    assert {options["think"] for _, options in calls} == {"high"}
    assert {options["num_ctx"] for _, options in calls} == {16384}


def test_ollama_num_ctx_grows_to_fit_the_prompt():
    assert _ollama_num_ctx("x" * 300, 32768) == 32768
    raised = _ollama_num_ctx("x" * 150_000, 32768)
    assert raised >= 150_000 // 3 and raised % 4096 == 0


def test_ollama_models_without_thinking_get_no_think_option(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    calls = _install_fake_ollama(monkeypatch, chain={"title": "T", "blocks": ["Scream 808"]})

    generate_chain_from_prompt(prompt="t", catalog=catalog, llm_model="llama3.2:latest")

    assert all(options["think"] is None for _, options in calls)


def test_reasoning_effort_support_and_resolution():
    assert supported_reasoning_efforts("openai", DEFAULT_OPENAI_MODEL) == REASONING_EFFORTS
    assert supported_reasoning_efforts("ollama", "gpt-oss:20b") == ("low", "medium", "high")
    assert supported_reasoning_efforts("ollama", "deepseek-r1:8b") == ("none", "low")

    assert resolve_reasoning_effort("openai", "gpt-5.6-luna", "none") == "none"
    assert resolve_reasoning_effort("ollama", "gpt-oss:20b", "none") == "low"
    assert resolve_reasoning_effort("ollama", "gpt-oss:20b", "xhigh") == "high"
    assert resolve_reasoning_effort("ollama", "deepseek-r1:8b", "high") == "low"
    with pytest.raises(LLMGenerationError):
        resolve_reasoning_effort("openai", DEFAULT_OPENAI_MODEL, "extreme")


def test_openai_offers_only_the_gpt_5_6_family():
    assert [model_id for model_id, _ in OPENAI_MODELS] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert DEFAULT_OPENAI_MODEL == "gpt-5.6-terra"
    assert list_llm_models("openai") == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


def test_openai_rejects_models_outside_the_offered_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("helixgen.llm._get_openai_client", lambda: object())

    with pytest.raises(LLMGenerationError, match="not supported"):
        _build_llm_caller(
            backend="openai",
            default_model="unused",
            endpoint="unused",
            openai_model="gpt-4o-mini",
        )


def test_list_llm_models_reads_installed_ollama_models(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []

    def fake_request(url: str, payload: Any, *, timeout: float) -> Any:
        seen.append(url)
        return {"models": [{"name": "llama3.2:latest"}, {"name": "gpt-oss:20b"}]}

    monkeypatch.setattr("helixgen.llm._ollama_request", fake_request)

    assert list_llm_models("ollama", "http://host:11434/api/generate") == [
        "gpt-oss:20b",
        "llama3.2:latest",
    ]
    assert seen == ["http://host:11434/api/tags"]


def test_generate_chain_with_openai_backend(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
) -> None:
    catalog = ModelCatalog(dataset_path)
    fake_client = object()
    seen: set[tuple[str, str]] = set()

    monkeypatch.setattr("helixgen.llm._OPENAI_CLIENT", None, raising=False)
    monkeypatch.setattr("helixgen.llm._get_openai_client", lambda: fake_client)

    def fake_call_openai(
        model_name: str, llm_request: LLMRequest, *, effort: str, client: Any | None = None
    ) -> str:
        assert client is fake_client
        seen.add((model_name, effort))
        if llm_request.schema_name == "parameters":
            return json.dumps({"blocks": {"block1": {}}})
        if llm_request.schema_name == "snapshots":
            return json.dumps(_default_snapshots())
        return json.dumps({"title": "AI Tone", "blocks": ["Horizon Drive"]})

    monkeypatch.setattr("helixgen.llm._call_openai", fake_call_openai)

    chain = generate_chain_from_prompt(
        prompt="describe via openai",
        catalog=catalog,
        llm_model="unused-default",
        backend="openai",
        openai_model="gpt-5.6-luna",
        reasoning_effort="none",
    )

    assert chain["meta"]["name"] == "AI Tone"
    assert chain["blocks"] == [{"model": "Horizon Drive"}]
    assert seen == {("gpt-5.6-luna", "none")}


def _snapshot_request(calls) -> LLMRequest:
    (request,) = [request for request, _ in calls if request.schema_name == "snapshots"]
    return request


class TestSnapshots:
    """The third round: three usable variations rather than one fixed sound."""

    CHAIN: ClassVar[dict[str, Any]] = {
        "title": "Stack",
        "blocks": ["Scream 808", "Brit Plexi Brt", "4x12 Greenback 25"],
    }

    def test_the_chain_carries_what_each_snapshot_changes(self, monkeypatch, dataset_path):
        catalog = ModelCatalog(dataset_path)
        _install_fake_ollama(
            monkeypatch,
            chain=self.CHAIN,
            parameters={"blocks": {"block2": {"Drive": 0.45}}},
            snapshots={
                "snapshots": [
                    {
                        "name": "Clean",
                        "blocks": {
                            "block1": {"enabled": False, "parameters": None},
                            "block2": {"enabled": None, "parameters": {"Drive": 0.2}},
                        },
                    },
                    {"name": "Rhythm", "blocks": {"block1": {"enabled": True}}},
                    {"name": "Solo", "blocks": {"block2": {"parameters": {"Drive": 0.6}}}},
                ]
            },
        )

        chain = generate_chain_from_prompt(
            prompt="rock", catalog=catalog, llm_model="fake-model"
        )

        # Block keys are one-based in the answer and zero-based indexes in a chain.
        assert chain["snapshots"] == [
            {"name": "Clean", "blocks": {"0": {"enabled": False}, "1": {"parameters": {"Drive": 0.2}}}},
            {"name": "Rhythm", "blocks": {"0": {"enabled": True}}},
            {"name": "Solo", "blocks": {"1": {"parameters": {"Drive": 0.6}}}},
        ]

    def test_the_prompt_shows_each_block_s_chosen_values(self, monkeypatch, dataset_path):
        catalog = ModelCatalog(dataset_path)
        calls = _install_fake_ollama(
            monkeypatch, chain=self.CHAIN, parameters={"blocks": {"block2": {"Drive": 0.45}}}
        )

        generate_chain_from_prompt(prompt="rock", catalog=catalog, llm_model="fake-model")

        prompt = _snapshot_request(calls).prompt
        assert "block2: Brit Plexi Brt" in prompt
        assert "now 0.45" in prompt
        # A cab keeps its defaults here too; it can only be switched.
        assert "block3: 4x12 Greenback 25 (Cab)\n- on/off only" in prompt

    def test_the_schema_pins_the_snapshot_count_and_name_length(self, dataset_path):
        catalog = ModelCatalog(dataset_path)
        summary = [
            entry
            for entry in _summarize_parameters(catalog.get("Scream 808"))
            if entry["name"] == "Gain"
        ]
        schema = _snapshots_schema({"block1": summary})

        snapshots = schema["properties"]["snapshots"]
        assert snapshots["minItems"] == snapshots["maxItems"] == SNAPSHOT_COUNT
        item = snapshots["items"]
        assert item["properties"]["name"]["maxLength"] == SNAPSHOT_NAME_LIMIT
        block = item["properties"]["blocks"]["properties"]["block1"]
        assert block["properties"]["enabled"] == {"type": ["boolean", "null"]}
        assert block["properties"]["parameters"]["properties"]["Gain"]["maximum"] == 1

    def test_a_parameter_no_controller_can_sweep_is_left_out(self, monkeypatch, dataset_path):
        """A snapshot value needs a range for the device's controller to sweep."""
        catalog = ModelCatalog(dataset_path)
        model = catalog.get("1x10 Princess Copperhead")
        assert model.get_parameter("Early Reflections").controller_range() is None

        calls = _install_fake_ollama(
            monkeypatch,
            chain={"title": "Cab", "blocks": ["Brit Plexi Brt", "1x10 Princess Copperhead"]},
        )
        generate_chain_from_prompt(prompt="rock", catalog=catalog, llm_model="fake-model")

        request = _snapshot_request(calls)
        assert "Early Reflections" not in request.prompt
        assert "Drive" in request.prompt

    @pytest.mark.parametrize(
        ("answer", "message"),
        [
            ({"snapshots": [{"name": "Only", "blocks": {}}]}, "exactly 3"),
            (
                {"snapshots": [{"name": "A", "blocks": {"block9": {}}}, {"name": "B", "blocks": {}}, {"name": "C", "blocks": {}}]},
                "unknown block",
            ),
            (
                {"snapshots": [{"name": "A", "blocks": {"block1": {"parameters": {"Bogus": 1}}}}, {"name": "B", "blocks": {}}, {"name": "C", "blocks": {}}]},
                "not valid",
            ),
            (
                {"snapshots": [{"name": "", "blocks": {}}, {"name": "B", "blocks": {}}, {"name": "C", "blocks": {}}]},
                "non-empty 'name'",
            ),
        ],
    )
    def test_an_answer_that_cannot_be_applied_is_rejected(
        self, monkeypatch, dataset_path, answer, message
    ):
        catalog = ModelCatalog(dataset_path)
        _install_fake_ollama(monkeypatch, chain=self.CHAIN, snapshots=answer)

        with pytest.raises(LLMGenerationError, match=message):
            generate_chain_from_prompt(prompt="rock", catalog=catalog, llm_model="fake-model")

    def test_a_rejected_answer_is_re_asked_once(self, monkeypatch, dataset_path):
        catalog = ModelCatalog(dataset_path)
        attempts: list[str] = []

        def answer(llm_request: LLMRequest) -> dict[str, Any]:
            attempts.append(llm_request.prompt)
            if len(attempts) == 1:
                return {"snapshots": [{"name": "Only", "blocks": {}}]}
            return _default_snapshots()

        _install_fake_ollama(monkeypatch, chain=self.CHAIN, snapshots=answer)

        chain = generate_chain_from_prompt(
            prompt="rock", catalog=catalog, llm_model="fake-model"
        )

        assert len(attempts) == 2
        assert "Your previous answer was rejected" in attempts[1]
        assert [snapshot["name"] for snapshot in chain["snapshots"]] == ["Snap 1", "Snap 2", "Snap 3"]
