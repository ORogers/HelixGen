"""Light pytest-qt smoke tests for the hlxgen_ui main window.

These check construction and signal wiring, not full generation/upload flows
end to end (that needs a live LLM backend and, for upload, a real pedal).

**Device access is always stubbed here.** The window must never make real USB
traffic just from being constructed or tested: that would make the suite
flaky and hardware-dependent, and a sweep walks someone's actual pedal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pytestqt")

from hlxgen_ui.device import DeviceSummary, SlotSummary
from hlxgen_ui.main_window import MainWindow
from hlxgen_ui.widgets.upload_panel import TRANSPORT_USB


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in an empty working directory.

    The catalog, template and schema now travel inside ``hlxgen.data`` and are
    found wherever the app runs from, so nothing has to be copied here. What
    still matters is the working directory: a generated preset lands in
    ``./generated-presets`` and would otherwise pollute the repo root.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_device(monkeypatch: pytest.MonkeyPatch):
    """Never let a test touch the real USB device, even by accident."""
    monkeypatch.setattr("hlxgen_ui.workers.find_device", lambda: None)
    monkeypatch.setattr(
        "hlxgen_ui.workers.read_slots",
        lambda count, bank=0, on_progress=None: [],
    )

    def _no_chain(slot, *, bank=0, dataset):
        from hlxgen_ui.device import DeviceUnavailable

        raise DeviceUnavailable("no device in tests")

    monkeypatch.setattr("hlxgen_ui.workers.read_slot_chain", _no_chain)


#: What the stubbed Ollama server reports: installed models and their thinking levels.
FAKE_OLLAMA_MODELS = {
    "gpt-oss:20b": ("low", "medium", "high"),
    "deepseek-r1:8b": ("none", "low"),
    "llama3.2:latest": (),
}


@pytest.fixture(autouse=True)
def no_real_ollama(monkeypatch: pytest.MonkeyPatch):
    """Opening Settings lists the installed models; never ask a real server."""
    monkeypatch.setattr(
        "hlxgen_ui.workers.list_llm_models", lambda backend, endpoint: list(FAKE_OLLAMA_MODELS)
    )
    monkeypatch.setattr(
        "hlxgen_ui.workers.supported_reasoning_efforts",
        lambda backend, model, endpoint=None: FAKE_OLLAMA_MODELS[model],
    )


def _stub_llm(
    monkeypatch: pytest.MonkeyPatch, *, title: str = "Smoke Test Tone"
) -> list[dict[str, object]]:
    """Answer both LLM rounds; returns the options each call was made with."""
    chain = json.dumps({"title": title, "blocks": ["Horizon Drive"]})
    params = json.dumps({"blocks": {"block1": {}}})
    calls: list[dict[str, object]] = []

    def fake_call(endpoint: str, model_name: str, llm_request, **options) -> str:
        calls.append({"model": model_name, **options})
        return chain if llm_request.schema_name == "chain" else params

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)
    return calls


def test_main_window_constructs_without_a_device(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "HelixPy"
    qtbot.waitUntil(
        lambda: "No pedal found" in window.slot_panel._status_label.text(), timeout=2000
    )


def test_refresh_with_no_device_shows_clean_state(qtbot):
    """Nothing attached: the sweep is never attempted, and the panel says so."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.refresh_requested.emit()
    qtbot.waitUntil(
        lambda: "No pedal found" in window.slot_panel._status_label.text(), timeout=2000
    )


def test_refresh_reports_a_failed_sweep(qtbot, monkeypatch: pytest.MonkeyPatch):
    """Device present but the sweep fails (busy, unplugged mid-read): the
    reason reaches the panel instead of a silent empty list."""
    monkeypatch.setattr(
        "hlxgen_ui.workers.find_device",
        lambda: DeviceSummary(description="HX Stomp (serial 3264140)", verified=True),
    )

    def unavailable(count, bank=0, on_progress=None):
        from hlxgen_ui.device import DeviceUnavailable

        raise DeviceUnavailable("HX Edit is holding the interface.")

    monkeypatch.setattr("hlxgen_ui.workers.read_slots", unavailable)

    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.refresh_requested.emit()
    qtbot.waitUntil(
        lambda: "HX Edit is holding" in window.slot_panel._status_label.text(), timeout=3000
    )


def test_refresh_lists_slots_and_names_them(qtbot, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "hlxgen_ui.workers.find_device",
        lambda: DeviceSummary(description="HX Stomp (serial 3264140)", verified=True),
    )
    monkeypatch.setattr(
        "hlxgen_ui.workers.read_slots",
        lambda count, bank=0, on_progress=None: [
            SlotSummary(index=0, populated=True, name="Warm Blues", detail="5 blocks"),
            SlotSummary(index=1, populated=False),
        ],
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.refresh_requested.emit()
    qtbot.waitUntil(lambda: window.slot_panel._list.count() == 2, timeout=2000)

    assert "Warm Blues" in window.slot_panel._list.item(0).text()
    assert "(empty)" in window.slot_panel._list.item(1).text()
    qtbot.waitUntil(
        lambda: "HX Stomp" in window.slot_panel._device_label.text(), timeout=2000
    )


def test_usb_upload_needs_a_selected_slot(qtbot, isolated_cwd, monkeypatch):
    _stub_llm(monkeypatch)
    window = MainWindow()
    qtbot.addWidget(window)

    window.prompt_panel._prompt_edit.setPlainText("warm bluesy crunch")
    window._on_generate()
    qtbot.waitUntil(lambda: window._last_result is not None, timeout=5000)

    # A preset exists but no slot is selected: USB upload must stay disabled,
    # because it would otherwise overwrite a slot nobody chose.
    assert window.upload_panel.transport() == TRANSPORT_USB
    assert not window.upload_panel._upload_button.isEnabled()

    window.slot_panel.slot_selected.emit(3)
    assert window.upload_panel._upload_button.isEnabled()


def test_usb_upload_sends_the_selected_slot(qtbot, isolated_cwd, monkeypatch):
    _stub_llm(monkeypatch)
    seen: dict[str, object] = {}

    def fake_push(preset, *, slot, bank=0, dataset, on_progress=None):
        seen["slot"] = slot
        seen["name"] = preset.get("data", {}).get("meta", {}).get("name")
        return "1 block swapped\ncommitted"

    monkeypatch.setattr("hlxgen_ui.workers.push_preset", fake_push)

    window = MainWindow()
    qtbot.addWidget(window)
    window.prompt_panel._prompt_edit.setPlainText("warm bluesy crunch")
    window._on_generate()
    qtbot.waitUntil(lambda: window._last_result is not None, timeout=5000)

    window.slot_panel.slot_selected.emit(7)
    window.upload_panel.upload_requested.emit(TRANSPORT_USB, "auto")
    qtbot.waitUntil(
        lambda: "Uploaded to slot 7" in window.upload_panel._status_label.text(), timeout=5000
    )
    assert seen["slot"] == 7


def test_generate_flow_populates_preview_and_upload_panel(
    qtbot, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
):
    _stub_llm(monkeypatch)

    window = MainWindow()
    qtbot.addWidget(window)

    window.prompt_panel._prompt_edit.setPlainText("warm bluesy crunch")
    window._on_generate()
    qtbot.waitUntil(lambda: window._last_result is not None, timeout=5000)

    assert window._last_result.output_path.exists()
    # The chain renders left to right as chips, one per block.
    assert window.preview_panel.chain_labels() == ["Horizon Drive"]


def test_generate_flow_reports_llm_failure(
    qtbot, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "hlxgen.llm._call_ollama", lambda endpoint, model_name, llm_request, **_: "not json"
    )

    window = MainWindow()
    qtbot.addWidget(window)

    window.prompt_panel._prompt_edit.setPlainText("anything")
    window._on_generate()
    qtbot.waitUntil(
        lambda: "Failed" in window.generation_panel._status_label.text(), timeout=5000
    )

    assert window._last_result is None


def test_close_while_a_worker_runs_does_not_tear_down_the_thread(
    qtbot, isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression: destroying a running QThread aborts the process.

    A slow LLM call used to outlive the window and take the whole app down
    with 'QThread: Destroyed while thread is still running'. The first close
    request must be refused while work is in flight.
    """
    import threading

    release = threading.Event()

    def slow_call(endpoint: str, model_name: str, llm_request, **_) -> str:
        release.wait(timeout=5)
        return json.dumps({"title": "Slow Tone", "blocks": ["Horizon Drive"]})

    monkeypatch.setattr("hlxgen.llm._call_ollama", slow_call)

    window = MainWindow()
    qtbot.addWidget(window)
    window.prompt_panel._prompt_edit.setPlainText("slow one")
    window._on_generate()
    qtbot.waitUntil(lambda: window._generation_worker.isRunning(), timeout=2000)

    try:
        # The close is refused outright while the worker is alive, so Qt never
        # gets to destroy the running QThread.
        assert window.close() is False
        assert window._generation_worker.isRunning()
    finally:
        # Never leave a live thread behind for teardown to destroy - that is
        # the very abort this test exists to prevent.
        release.set()
        qtbot.waitUntil(lambda: not window._generation_worker.isRunning(), timeout=10000)


def _generate(qtbot, window, prompt: str = "warm bluesy crunch") -> None:
    window.prompt_panel._prompt_edit.setPlainText(prompt)
    window._on_generate()
    qtbot.waitUntil(lambda: window._last_result is not None, timeout=5000)


def test_auto_upload_sends_to_the_selected_slot(qtbot, isolated_cwd, monkeypatch):
    """The whole point of the toggle: generate, and it lands on the pedal."""
    _stub_llm(monkeypatch)
    sent: dict[str, object] = {}

    def fake_push(preset, *, slot, bank=0, dataset, on_progress=None):
        sent["slot"] = slot
        return "committed"

    monkeypatch.setattr("hlxgen_ui.workers.push_preset", fake_push)

    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.slot_selected.emit(4)  # chosen *before* generating
    assert window.upload_panel.auto_upload_enabled()

    _generate(qtbot, window)
    qtbot.waitUntil(lambda: sent.get("slot") == 4, timeout=5000)


def test_auto_upload_never_guesses_a_slot(qtbot, isolated_cwd, monkeypatch):
    """No slot selected means no write - an auto-upload must not pick one."""
    _stub_llm(monkeypatch)
    calls: list[int] = []

    def fake_push(preset, *, slot, bank=0, dataset, on_progress=None):
        calls.append(slot)
        return "committed"

    monkeypatch.setattr("hlxgen_ui.workers.push_preset", fake_push)

    window = MainWindow()
    qtbot.addWidget(window)
    _generate(qtbot, window)
    qtbot.wait(300)

    assert calls == []
    assert "Pick a slot" in window.upload_panel._status_label.text()


def test_auto_upload_respects_the_toggle(qtbot, isolated_cwd, monkeypatch):
    _stub_llm(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(
        "hlxgen_ui.workers.push_preset",
        lambda preset, *, slot, bank=0, dataset, on_progress=None: calls.append(slot),
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.upload_panel._auto_upload.setChecked(False)
    window.slot_panel.slot_selected.emit(4)

    _generate(qtbot, window)
    qtbot.wait(300)
    assert calls == []


def test_settings_page_drives_generation(qtbot):
    """Model choice lives on the settings page, not the prompt panel."""
    window = MainWindow()
    qtbot.addWidget(window)

    assert not hasattr(window.prompt_panel, "_backend_combo")

    window.settings_page._ollama_model.setEditText("llama3.2:latest")
    window.settings_page._slot_count.setValue(32)
    assert window._settings.ollama_model == "llama3.2:latest"
    assert window._settings.slot_count == 32


def _open_settings(qtbot, window) -> None:
    window.show()
    window._settings_button.click()
    qtbot.waitUntil(
        lambda: window.settings_page._ollama_model.count() == len(FAKE_OLLAMA_MODELS),
        timeout=3000,
    )


def test_settings_lists_installed_ollama_models(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    _open_settings(qtbot, window)

    combo = window.settings_page._ollama_model
    assert [combo.itemText(i) for i in range(combo.count())] == list(FAKE_OLLAMA_MODELS)
    # The typed choice survives the list arriving.
    assert combo.currentText() == "gpt-oss:20b"


def test_thinking_level_follows_what_the_model_supports(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    _open_settings(qtbot, window)
    page = window.settings_page

    def enabled_levels() -> list[str]:
        model = page._thinking.model()
        return [
            page._thinking.itemData(row)
            for row in range(page._thinking.count())
            if model.item(row).isEnabled()
        ]

    assert enabled_levels() == ["low", "medium", "high"]
    assert window._settings.reasoning_effort == "low"

    # A level gpt-oss doesn't offer is greyed out, so picking Max from the
    # OpenAI list and switching back lands on the nearest one it does offer.
    page._backend_combo.setCurrentIndex(page._backend_combo.findData("openai"))
    assert enabled_levels() == ["none", "low", "medium", "high", "xhigh", "max"]
    page._thinking.setCurrentIndex(page._thinking.findData("max"))
    assert window._settings.reasoning_effort == "max"

    page._backend_combo.setCurrentIndex(page._backend_combo.findData("ollama"))
    assert window._settings.reasoning_effort == "high"
    assert page._thinking.currentText() == "High"
    assert "doesn't offer Max" in page._thinking_hint.text()

    page._ollama_model.setEditText("llama3.2:latest")
    assert not page._thinking.isEnabled()
    assert "doesn't think" in page._thinking_hint.text()


def test_openai_offers_only_the_gpt_5_6_models(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    page = window.settings_page

    page._backend_combo.setCurrentIndex(page._backend_combo.findData("openai"))
    offered = [page._openai_model.itemData(i) for i in range(page._openai_model.count())]
    assert offered == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert window._settings.openai_model == "gpt-5.6-terra"

    page._openai_model.setCurrentIndex(page._openai_model.findData("gpt-5.6-luna"))
    assert window._settings.openai_model == "gpt-5.6-luna"


def test_thinking_level_and_context_reach_the_llm(qtbot, isolated_cwd, monkeypatch):
    calls = _stub_llm(monkeypatch)
    window = MainWindow()
    qtbot.addWidget(window)
    page = window.settings_page

    page._thinking.setCurrentIndex(page._thinking.findData("medium"))
    page._num_ctx.setValue(16384)
    _generate(qtbot, window)

    assert [call["think"] for call in calls] == ["medium", "medium"]
    assert [call["num_ctx"] for call in calls] == [16384, 16384]


def test_settings_button_swaps_pages(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window._pages.currentIndex() == 0
    window._settings_button.click()
    assert window._pages.currentIndex() == 1
    assert window._settings_button.text() == "Back"
    window._settings_button.click()
    assert window._pages.currentIndex() == 0


def test_selecting_a_slot_shows_its_chain(qtbot, monkeypatch: pytest.MonkeyPatch):
    """Clicking a slot reads it and draws what is in it, left to right."""
    from hlxgen_ui.device import ChainBlock, SlotChain

    monkeypatch.setattr(
        "hlxgen_ui.workers.read_slot_chain",
        lambda slot, *, bank=0, dataset: SlotChain(
            slot=slot,
            name="Vox AC30 TS Spac",
            blocks=[
                ChainBlock(position=1, name="Scream 808", category="Distortion"),
                ChainBlock(position=2, name="Essex A30", category="Amp"),
                ChainBlock(position=2, name="2x12 Silver Bell", category="Cab", fused_cab=True),
            ],
        ),
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.slot_selected.emit(1)

    qtbot.waitUntil(lambda: len(window.preview_panel.chain_labels()) == 3, timeout=3000)
    assert window.preview_panel.chain_labels() == [
        "Scream 808",
        "Essex A30",
        "2x12 Silver Bell",
    ]
    assert "Vox AC30 TS Spac" in window.preview_panel._title.text()


def test_an_empty_slot_says_so_rather_than_showing_a_stale_chain(qtbot, monkeypatch):
    from hlxgen_ui.device import SlotChain

    monkeypatch.setattr(
        "hlxgen_ui.workers.read_slot_chain",
        lambda slot, *, bank=0, dataset: SlotChain(slot=slot, name=None, blocks=[]),
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.slot_selected.emit(9)
    qtbot.waitUntil(
        lambda: "empty" in window.preview_panel._empty_label.text(), timeout=3000
    )
    assert window.preview_panel.chain_labels() == []


def test_a_failed_slot_read_is_reported_in_the_preview(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    window.slot_panel.slot_selected.emit(2)
    qtbot.waitUntil(
        lambda: "Could not read" in window.preview_panel._empty_label.text(), timeout=3000
    )


def test_upload_is_refused_and_explained_without_hx_edit(qtbot, isolated_cwd, monkeypatch):
    """No HX Edit, no upload - said up front, not raised mid-write.

    Helix.sym lives inside HX Edit and is Line 6's to distribute, so a USB
    write cannot address a model without a local install. This used to fail
    inside the upload worker with whatever exception surfaced first.
    """
    from hlxgen.device import hxedit

    monkeypatch.setattr(hxedit, "find_hx_edit", lambda explicit=None: None)
    _stub_llm(monkeypatch)
    pushed: list[int] = []
    monkeypatch.setattr(
        "hlxgen_ui.workers.push_preset",
        lambda preset, *, slot, bank=0, dataset, on_progress=None: pushed.append(slot),
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.upload_panel.set_selected_slot(3)
    _generate(qtbot, window)
    qtbot.wait(300)

    assert pushed == [], "a preset was written with no symbol table to address it"
    assert not window.upload_panel._upload_button.isEnabled()
    assert "HX Edit" in window.upload_panel._upload_button.toolTip()
    assert "HX Edit" in window.upload_panel._status_label.text()
