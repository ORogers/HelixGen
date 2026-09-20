"""Main window: wires the five panels to the generation, slot-read and
upload workers.

Layout follows the plan's UX guidance: the device and its slots on the left
(persistent context), prompt then generation then preview stacked in the
centre (the primary flow, top to bottom in the order the user works through
it), upload on the right (a distinct, clearly separate final step).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hlxgen.cli import DEFAULT_DATASET
from hlxgen.dataset import ModelCatalog
from hlxgen.device import hxedit

from .device import chain_from_preset
from .generation import GenerationOptions, GenerationResult
from .settings import Settings
from .widgets import (
    GenerationPanel,
    PreviewPanel,
    PromptPanel,
    SettingsPage,
    SlotPanel,
    UploadPanel,
)
from .widgets.upload_panel import TRANSPORT_USB
from .workers import (
    DeviceScanWorker,
    GenerationWorker,
    ScriptUploadWorker,
    SlotChainWorker,
    SlotReadWorker,
    UsbUploadWorker,
    live_workers,
)


class MainWindow(QMainWindow):
    def __init__(self, *, dataset_path: Path = DEFAULT_DATASET) -> None:
        super().__init__()
        self.setWindowTitle("hlxgen")
        self.resize(1320, 800)
        self.setMinimumSize(980, 620)

        self._dataset_path = dataset_path
        self._settings = Settings()
        self._catalog: ModelCatalog | None = None
        self._last_result: GenerationResult | None = None
        self._selected_slot: int | None = None
        self._force_close = False
        self._pending_read = False

        self._generation_worker: GenerationWorker | None = None
        self._scan_worker: DeviceScanWorker | None = None
        self._slot_worker: SlotReadWorker | None = None
        self._chain_worker: SlotChainWorker | None = None
        self._pending_chain_slot: int | None = None
        self._upload_worker: UsbUploadWorker | ScriptUploadWorker | None = None

        self.slot_panel = SlotPanel()
        self.prompt_panel = PromptPanel()
        self.generation_panel = GenerationPanel()
        self.preview_panel = PreviewPanel()
        self.upload_panel = UploadPanel()

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(16)
        center_layout.addWidget(self.prompt_panel)
        center_layout.addWidget(self.generation_panel, stretch=1)

        columns = QSplitter()
        columns.setHandleWidth(20)
        columns.setChildrenCollapsible(False)
        columns.addWidget(self.slot_panel)
        columns.addWidget(center)
        columns.addWidget(self.upload_panel)
        columns.setStretchFactor(0, 1)
        columns.setStretchFactor(1, 3)
        columns.setStretchFactor(2, 1)

        # The chain runs along the bottom, the full width of the window: it is
        # read left to right like the signal it describes, so it wants length
        # rather than the narrow column the centre could give it.
        workspace = QSplitter(Qt.Orientation.Vertical)
        workspace.setHandleWidth(20)
        workspace.setChildrenCollapsible(False)
        workspace.addWidget(columns)
        workspace.addWidget(self.preview_panel)
        workspace.setStretchFactor(0, 3)
        workspace.setStretchFactor(1, 2)
        workspace.setSizes([440, 260])

        self.settings_page = SettingsPage(self._settings)
        self.settings_page.closed.connect(self._show_workspace)
        # Helix.sym lives inside HX Edit, so no install means no upload. The
        # settings page is what knows, and it re-checks every time it opens.
        self.settings_page.hx_edit_changed.connect(
            lambda bundle: self.upload_panel.set_hx_edit_available(bundle is not None)
        )
        self.upload_panel.set_hx_edit_available(hxedit.find_hx_edit() is not None)

        self._pages = QStackedWidget()
        self._pages.addWidget(workspace)
        self._pages.addWidget(self.settings_page)

        self._settings_button = QPushButton("Settings")
        self._settings_button.clicked.connect(self._toggle_settings)

        container = QWidget()
        container.setObjectName("appBackground")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(18)
        outer.addWidget(_build_header(self._settings_button))
        outer.addWidget(self._pages, stretch=1)
        self.setCentralWidget(container)

        self.slot_panel.refresh_requested.connect(self._on_refresh_slots)
        self.slot_panel.slot_selected.connect(self._on_slot_selected)
        self.prompt_panel.generate_requested.connect(self._on_generate)
        self.upload_panel.upload_requested.connect(self._on_upload)

        # Enumeration only - it never opens a session or sends the pedal a
        # single byte, so it is safe to do unprompted and tells the user
        # straight away whether anything is plugged in. Reading the slots
        # themselves stays behind Refresh.
        self._scan_for_device()

    # -- pages ---------------------------------------------------------

    def _toggle_settings(self) -> None:
        if self._pages.currentIndex() == 0:
            self._pages.setCurrentIndex(1)
            self._settings_button.setText("Back")
        else:
            self._show_workspace()

    def _show_workspace(self) -> None:
        self._pages.setCurrentIndex(0)
        self._settings_button.setText("Settings")

    # -- catalog (lazy - a missing/invalid dataset shouldn't block the whole
    #    window from opening, only preview rendering) -----------------------

    def _catalog_or_none(self) -> ModelCatalog | None:
        if self._catalog is None:
            try:
                self._catalog = ModelCatalog(self._dataset_path)
            except Exception as exc:  # noqa: BLE001 - surfaced in the panel instead
                self.generation_panel.finish_failure(f"Could not load model catalog: {exc}")
                return None
        return self._catalog

    # -- device ------------------------------------------------------------

    def _scan_for_device(self, *, then_read: bool = False) -> None:
        """Enumerate, and optionally read the slots once we know what's there.

        The two run in sequence rather than together: a scan finishing after a
        sweep would otherwise overwrite the slot list with its own "connected"
        message, which is exactly the race an earlier version shipped.

        A refresh that arrives while the startup scan is still in flight is
        *remembered*, not dropped - otherwise clicking Refresh in the first
        moment after launch silently does nothing.
        """
        if then_read:
            self._pending_read = True
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self.slot_panel.set_scanning()
        self._scan_worker = DeviceScanWorker()
        self._scan_worker.found.connect(self._on_device_found)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.start()

    def _on_device_found(self, device: object) -> None:
        wanted_read = self._pending_read
        self._pending_read = False
        self.slot_panel.set_device(device)  # type: ignore[arg-type]
        if device is None:
            self.slot_panel.set_no_device(
                "No pedal found. Connect it over USB and click Refresh."
            )
            return
        if wanted_read:
            self._start_slot_read()
        else:
            self.slot_panel.set_hint("Click Refresh to read what's in each slot.")

    def _on_scan_failed(self, message: str) -> None:
        self._pending_read = False
        self.slot_panel.set_no_device(message)

    def _start_slot_read(self) -> None:
        if self._slot_worker is not None and self._slot_worker.isRunning():
            return
        self.slot_panel.set_reading()
        self._slot_worker = SlotReadWorker(self._settings.slot_count)
        self._slot_worker.progress.connect(self.slot_panel.set_reading)
        self._slot_worker.succeeded.connect(self.slot_panel.set_slots)
        self._slot_worker.failed.connect(self.slot_panel.set_no_device)
        self._slot_worker.finished.connect(self._maybe_close_after_work)
        self._slot_worker.start()

    def _on_refresh_slots(self) -> None:
        if self._slot_worker is not None and self._slot_worker.isRunning():
            return
        self._scan_for_device(then_read=True)

    def _on_slot_selected(self, slot_index: int) -> None:
        self._selected_slot = slot_index
        self.upload_panel.set_selected_slot(slot_index)
        self._show_slot_chain(slot_index)

    def _show_slot_chain(self, slot_index: int) -> None:
        """Read the chosen slot and draw what is in it.

        Read-only, and the newest click wins: clicking down a list must not
        queue a read per row, nor leave an earlier slot's chain on screen
        after a later one was asked for.
        """
        self._pending_chain_slot = slot_index
        if self._chain_worker is not None and self._chain_worker.isRunning():
            return

        self.preview_panel.show_message(f"Slot {slot_index}", "Reading from the pedal...")
        worker = SlotChainWorker(slot_index, dataset=self._dataset_path)
        worker.succeeded.connect(self._on_slot_chain)
        worker.failed.connect(
            lambda message: self.preview_panel.show_message(
                f"Slot {self._pending_chain_slot}", f"Could not read this slot: {message}"
            )
        )
        worker.finished.connect(self._after_chain_read)
        self._chain_worker = worker
        worker.start()

    def _on_slot_chain(self, chain: object) -> None:
        title = f"Slot {chain.slot}"
        if chain.name:
            title += f" - {chain.name}"
        self.preview_panel.show_chain(title, chain.blocks)

    def _after_chain_read(self) -> None:
        """Serve the most recent selection if it changed mid-read."""
        self._maybe_close_after_work()
        worker, self._chain_worker = self._chain_worker, None
        if worker is not None and self._pending_chain_slot != worker.slot:
            self._show_slot_chain(self._pending_chain_slot)

    # -- generation ------------------------------------------------------

    def _on_generate(self) -> None:
        if self._generation_worker is not None and self._generation_worker.isRunning():
            return
        prompt = self.prompt_panel.prompt_text()
        if not prompt:
            return

        options = GenerationOptions(
            prompt=prompt,
            dataset=self._dataset_path,
            backend=self._settings.backend,
            ollama_model=self._settings.ollama_model,
            ollama_endpoint=self._settings.ollama_endpoint,
            openai_model=self._settings.openai_model,
            reasoning_effort=self._settings.reasoning_effort,
            num_ctx=self._settings.num_ctx,
        )

        self.prompt_panel.set_busy(True)
        self.generation_panel.start()
        self.preview_panel.clear()
        self.upload_panel.set_generated_preset(None)

        self._generation_worker = GenerationWorker(options)
        self._generation_worker.progress.connect(self.generation_panel.add_step)
        self._generation_worker.succeeded.connect(self._on_generation_succeeded)
        self._generation_worker.failed.connect(self._on_generation_failed)
        # `finished` rather than the result signals: those fire from inside
        # run(), while the thread is still alive, so a close waiting on it
        # would look at a still-running worker and keep waiting forever.
        self._generation_worker.finished.connect(self._maybe_close_after_work)
        self._generation_worker.start()

    def _on_generation_succeeded(self, result: GenerationResult) -> None:
        self._last_result = result
        self.prompt_panel.set_busy(False)
        self.generation_panel.finish_success(f"Done - wrote {result.output_path.name}")
        for warning in result.warnings:
            self.generation_panel.add_step(f"Warning: {warning}")
        catalog = self._catalog_or_none()
        if catalog is not None:
            name = result.preset.get("data", {}).get("meta", {}).get("name") or "Generated"
            self.preview_panel.show_chain(
                f"Generated - {name}", chain_from_preset(result.preset, catalog)
            )
        self.upload_panel.set_generated_preset(result.output_path)
        self._maybe_auto_upload()

    def _on_generation_failed(self, message: str) -> None:
        self.prompt_panel.set_busy(False)
        self.generation_panel.finish_failure(message)

    # -- upload ------------------------------------------------------------

    def _maybe_auto_upload(self) -> None:
        """Send a freshly generated tone straight to the chosen slot.

        Only fires when the user has both ticked the box and picked a slot:
        the upload overwrites that slot, so it must never be able to guess
        one. The AppleScript transport is excluded because it cannot target a
        slot at all - its 'auto' mode always lands on slot 1, which is not
        something to do without a deliberate click.
        """
        if not self.upload_panel.auto_upload_enabled():
            return
        if self.upload_panel.transport() != TRANSPORT_USB:
            return
        if not self.upload_panel.hx_edit_available():
            self.upload_panel.set_progress(
                "Generated, but HX Edit was not found - set it under Settings."
            )
            return
        if self._selected_slot is None:
            self.upload_panel.set_progress(
                "Generated. Pick a slot to upload to - auto-upload needs a target."
            )
            return
        self.generation_panel.add_step(f"Auto-uploading to slot {self._selected_slot}...")
        self._on_upload(TRANSPORT_USB, self.upload_panel.mode())

    def _on_upload(self, transport: str, mode: str) -> None:
        if self._last_result is None:
            return
        if self._upload_worker is not None and self._upload_worker.isRunning():
            return

        self.upload_panel.set_busy(True)
        if transport == TRANSPORT_USB:
            if self._selected_slot is None:
                self.upload_panel.set_failure("No target slot selected.")
                return
            worker: UsbUploadWorker | ScriptUploadWorker = UsbUploadWorker(
                self._last_result.preset,
                slot=self._selected_slot,
                dataset=self._dataset_path,
            )
            worker.progress.connect(self.upload_panel.set_progress)
            worker.succeeded.connect(self._on_usb_upload_succeeded)
        else:
            worker = ScriptUploadWorker(self._last_result.output_path, mode=mode)
            worker.succeeded.connect(
                lambda: self.upload_panel.set_success("Triggered HX Edit upload.")
            )
        worker.failed.connect(self.upload_panel.set_failure)
        worker.finished.connect(self._maybe_close_after_work)
        self._upload_worker = worker
        worker.start()

    def _on_usb_upload_succeeded(self, report: str) -> None:
        slot = self._selected_slot
        self.upload_panel.set_success(f"Uploaded to slot {slot}.")
        for line in report.splitlines():
            if line.strip():
                self.generation_panel.add_step(line.rstrip())

    # -- shutdown ------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Never let Qt tear the window down while a worker is still running.

        Destroying a running QThread aborts the process outright - which is
        what a hung LLM call used to cause here ("QThread: Destroyed while
        thread is still running"). So a close request that arrives mid-work is
        refused, and the window closes itself the moment the work lands.

        A second request hides the window so it *looks* closed, but still does
        not kill the thread: ``QThread.terminate()`` on a thread executing
        Python can leave the GIL held and hang the whole interpreter, which is
        a worse outcome than waiting. Every worker is bounded (the LLM call by
        ``OLLAMA_TIMEOUT_SECONDS``, USB work by the transport's own timeouts),
        so the wait always ends.
        """
        if not live_workers():
            super().closeEvent(event)
            return

        if not self._force_close:
            self._force_close = True
            self.generation_panel.add_step(
                "Finishing current work before closing - close again to hide."
            )
        else:
            self.hide()
        event.ignore()

    def _maybe_close_after_work(self) -> None:
        """Close for real once the work a close request was waiting on ends."""
        if self._force_close and not live_workers():
            self.close()


def _build_header(settings_button: QWidget) -> QWidget:
    """App title + subtitle on the left, the settings toggle on the right."""
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(4, 0, 4, 0)

    text = QVBoxLayout()
    text.setSpacing(2)

    title = QLabel("hlxgen")
    title.setObjectName("appTitle")

    subtitle = QLabel("Describe a tone, generate a Helix preset, send it to your pedal.")
    subtitle.setObjectName("appSubtitle")

    text.addWidget(title)
    text.addWidget(subtitle)

    row.addLayout(text)
    row.addStretch(1)
    row.addWidget(settings_button)
    return header
