"""Tests for the optional `on_progress` callback added to
`generate_chain_from_prompt` for helixgen_ui's generation panel.

These are a regression guard as much as anything: `on_progress` must be
purely additive, so `generate_chain_from_prompt`'s existing behaviour (and
the whole CLI `describe` path built on top of it) is unaffected when it's
omitted.
"""

import json
import re
from pathlib import Path

import pytest

from helixgen.dataset import ModelCatalog
from helixgen.llm import LLMRequest, generate_chain_from_prompt


def _fake_call(endpoint: str, model_name: str, llm_request: LLMRequest, **_: object) -> str:
    _ = endpoint, model_name
    if llm_request.schema_name == "parameters":
        return json.dumps({"blocks": {"block1": {}}})
    if llm_request.schema_name == "snapshots":
        return json.dumps(
            {"snapshots": [{"name": name, "blocks": {}} for name in ("Clean", "Crunch", "Lead")]}
        )
    return json.dumps({"title": "Clean Tone", "blocks": ["Horizon Drive"]})


def test_on_progress_omitted_is_a_no_op(monkeypatch: pytest.MonkeyPatch, dataset_path: Path):
    """Existing (CLI) call sites that don't pass on_progress see no change."""
    catalog = ModelCatalog(dataset_path)
    monkeypatch.setattr("helixgen.llm._call_ollama", _fake_call)

    chain = generate_chain_from_prompt(
        prompt="clean tone",
        catalog=catalog,
        llm_model="fake-model",
    )

    assert chain["meta"]["name"] == "Clean Tone"


def test_on_progress_receives_ordered_status_messages(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
):
    catalog = ModelCatalog(dataset_path)
    monkeypatch.setattr("helixgen.llm._call_ollama", _fake_call)

    messages: list[str] = []
    generate_chain_from_prompt(
        prompt="clean tone",
        catalog=catalog,
        llm_model="fake-model",
        on_progress=messages.append,
    )

    assert messages[0] == "Requesting initial block selection..."
    assert any(m.startswith("Selected 1 block(s): Horizon Drive (") for m in messages)
    assert "Setting parameters for 1 block(s)..." in messages
    assert any(m.startswith("Parameters set for 1 block(s) (") for m in messages)
    assert "Designing 3 snapshots..." in messages
    assert any(m.startswith("Snapshots: Clean, Crunch, Lead (") for m in messages)
    # Each LLM step and the whole run report how long they took.
    assert re.fullmatch(r"Chain generation complete \(\d+\.\ds total\)\.", messages[-1])


def test_on_progress_reports_cab_block_skip(
    monkeypatch: pytest.MonkeyPatch, dataset_path: Path
):
    catalog = ModelCatalog(dataset_path)
    cab_model = next(
        (m for m in catalog.models() if "cab" in (m.category or "").lower()), None
    )
    if cab_model is None:
        pytest.skip("dataset has no cab-category model to exercise the skip path")

    def fake_call(endpoint: str, model_name: str, llm_request: LLMRequest, **_: object) -> str:
        _ = endpoint, model_name
        if llm_request.schema_name == "parameters":
            return json.dumps({"blocks": {}})
        if llm_request.schema_name == "snapshots":
            return json.dumps(
                {"snapshots": [{"name": "S", "blocks": {}} for _ in range(3)]}
            )
        return json.dumps({"title": "Cab Test", "blocks": [cab_model.display_name]})

    monkeypatch.setattr("helixgen.llm._call_ollama", fake_call)

    messages: list[str] = []
    generate_chain_from_prompt(
        prompt="cab only",
        catalog=catalog,
        llm_model="fake-model",
        on_progress=messages.append,
    )

    assert any("cab defaults used" in m for m in messages)
