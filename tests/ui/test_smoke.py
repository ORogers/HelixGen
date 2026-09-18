"""Light pytest-qt smoke tests for the hlxgen_ui main window.

These check construction and signal wiring, not full generation/upload flows
end to end (that needs a live LLM backend and, for upload, a real pedal).

**Device access is always stubbed here.** The window must never make real USB
traffic just from being constructed or tested: that would make the suite
flaky and hardware-dependent, and a sweep walks someone's actual pedal.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("pytestqt")

from hlxgen_ui.device import DeviceSummary, SlotSummary
from hlxgen_ui.main_window import MainWindow
from hlxgen_ui.widgets.upload_panel import TRANSPORT_USB


@pytest.fixture
def project_dataset_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the files ModelCatalog/generate_preset need into an isolated cwd,
    so tests don't depend on (or pollute) the repo root."""
    project_root = Path(__file__).resolve().parent.parent.parent
    for name in ("helix_model_information.json", "HXTemplate.hlx", "helix-preset.schema.json"):
        shutil.copy(project_root / name, tmp_path / name)
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


def _stub_llm(monkeypatch: pytest.MonkeyPatch, *, title: str = "Smoke Test Tone") -> None:
    chain = json.dumps({"title": title, "blocks": ["Horizon Drive"]})
    params = json.dumps({"parameters": {}})
    calls: list[str] = []

    def fake_call(endpoint: str, model_name: str, prompt: str) -> str:
        calls.append(prompt)
        return chain if len(calls) == 1 else params

    monkeypatch.setattr("hlxgen.llm._call_ollama", fake_call)


def test_main_window_constructs_without_a_device(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "hlxgen"
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


def test_usb_upload_needs_a_selected_slot(qtbot, project_dataset_files, monkeypatch):
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


def test_usb_upload_sends_the_selected_slot(qtbot, project_dataset_files, monkeypatch):
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
    qtbot, project_dataset_files: Path, monkeypatch: pytest.MonkeyPatch
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
    qtbot, project_dataset_files: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "hlxgen.llm._call_ollama", lambda endpoint, model_name, prompt: "not json"
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
    qtbot, project_dataset_files: Path, monkeypatch: pytest.MonkeyPatch
):
    """Regression: destroying a running QThread aborts the process.

    A slow LLM call used to outlive the window and take the whole app down
    with 'QThread: Destroyed while thread is still running'. The first close
    request must be refused while work is in flight.
    """
    import threading

    release = threading.Event()

    def slow_call(endpoint: str, model_name: str, prompt: str) -> str:
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


def test_auto_upload_sends_to_the_selected_slot(qtbot, project_dataset_files, monkeypatch):
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


def test_auto_upload_never_guesses_a_slot(qtbot, project_dataset_files, monkeypatch):
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


def test_auto_upload_respects_the_toggle(qtbot, project_dataset_files, monkeypatch):
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

    window.settings_page._ollama_model.setText("llama3.2:latest")
    window.settings_page._slot_count.setValue(32)
    assert window._settings.ollama_model == "llama3.2:latest"
    assert window._settings.slot_count == 32


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
