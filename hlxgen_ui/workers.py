"""Background QThread workers.

LLM calls, model listing, USB reads and uploads are all blocking I/O, so none
of them may run on the Qt UI thread - each gets its own QThread here that emits
Qt signals back to the main thread instead of returning a value directly.

**Every live worker is also held in :data:`_LIVE_WORKERS`.** Destroying a
QThread object while its thread is still running aborts the process, and at
interpreter teardown Python will happily collect a worker whose only reference
was a closed window's attribute. Keeping a module-level reference until
``finished`` fires removes that whole class of crash.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from hlxgen.dataset import ModelCatalogError
from hlxgen.llm import (
    LLMGenerationError,
    list_llm_models,
    supported_reasoning_efforts,
    verify_openai_api_key,
)
from hlxgen.validator import ValidationError

from .device import (
    DeviceUnavailable,
    SlotChain,
    SlotSummary,
    find_device,
    push_preset,
    read_slot_chain,
    read_slots,
)
from .generation import (
    GenerationOptions,
    GenerationResult,
    GenerationValidationError,
    generate_tone,
)
from .upload import upload_preset

#: Workers that are running or have not yet reported. See the module docstring.
_LIVE_WORKERS: set[QThread] = set()


class _Worker(QThread):
    """Shared lifetime handling for every worker in this module."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        _LIVE_WORKERS.add(self)
        self.finished.connect(lambda: _LIVE_WORKERS.discard(self))


def live_workers() -> set[QThread]:
    """Workers still running, for the main window's shutdown handling."""
    return {worker for worker in _LIVE_WORKERS if worker.isRunning()}


class GenerationWorker(_Worker):
    """Runs :func:`hlxgen_ui.generation.generate_tone` off the UI thread."""

    progress = Signal(str)
    succeeded = Signal(object)  # GenerationResult
    failed = Signal(str)

    def __init__(self, options: GenerationOptions, parent=None) -> None:
        super().__init__(parent)
        self._options = options

    def run(self) -> None:
        try:
            result: GenerationResult = generate_tone(
                self._options, on_progress=self.progress.emit
            )
        except (
            LLMGenerationError,
            GenerationValidationError,
            ModelCatalogError,
            ValidationError,
            OSError,
        ) as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class ModelListWorker(_Worker):
    """Lists the models installed on an Ollama server, with the thinking levels
    each one supports. Both answers come from the server, so this runs off the
    UI thread."""

    succeeded = Signal(list)  # list[tuple[str, tuple[str, ...]]]
    failed = Signal(str)

    def __init__(self, endpoint: str, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint

    def run(self) -> None:
        try:
            models = [
                (model, supported_reasoning_efforts("ollama", model, self._endpoint))
                for model in list_llm_models("ollama", self._endpoint)
            ]
        except LLMGenerationError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(models)


class ApiKeyTestWorker(_Worker):
    """Checks an OpenAI key is accepted. A network round trip, so off-thread."""

    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, key: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key

    def run(self) -> None:
        try:
            verify_openai_api_key(self._key)
        except LLMGenerationError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit()


class DeviceScanWorker(_Worker):
    """Enumerates attached pedals - fast, and never opens a session."""

    found = Signal(object)  # DeviceSummary | None
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.found.emit(find_device())
        except DeviceUnavailable as exc:
            self.failed.emit(str(exc))


class SlotReadWorker(_Worker):
    """Sweeps device slots off the UI thread."""

    progress = Signal(str)
    succeeded = Signal(list)  # list[SlotSummary]
    failed = Signal(str)

    def __init__(self, count: int, *, bank: int = 0, parent=None) -> None:
        super().__init__(parent)
        self._count = count
        self._bank = bank

    def run(self) -> None:
        try:
            slots: list[SlotSummary] = read_slots(
                self._count, bank=self._bank, on_progress=self.progress.emit
            )
        except DeviceUnavailable as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a bad sweep must not kill the UI
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(slots)


class SlotChainWorker(_Worker):
    """Reads one slot and decodes its chain, off the UI thread."""

    succeeded = Signal(object)  # SlotChain
    failed = Signal(str)

    def __init__(self, slot: int, *, dataset: Path, bank: int = 0, parent=None) -> None:
        super().__init__(parent)
        self._slot = slot
        self._dataset = dataset
        self._bank = bank

    @property
    def slot(self) -> int:
        return self._slot

    def run(self) -> None:
        try:
            chain: SlotChain = read_slot_chain(
                self._slot, bank=self._bank, dataset=self._dataset
            )
        except DeviceUnavailable as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a bad read must not kill the UI
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(chain)


class UsbUploadWorker(_Worker):
    """Puts a generated preset into a chosen slot over USB.

    Same path as ``hlxgen push``: surgical edits via ``apply_tone``.
    """

    progress = Signal(str)
    succeeded = Signal(str)  # the push report
    failed = Signal(str)

    def __init__(
        self,
        preset: dict,
        *,
        slot: int,
        dataset: Path,
        bank: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._preset = preset
        self._slot = slot
        self._bank = bank
        self._dataset = dataset

    def run(self) -> None:
        try:
            report = push_preset(
                self._preset,
                slot=self._slot,
                bank=self._bank,
                dataset=self._dataset,
                on_progress=self.progress.emit,
            )
        except Exception as exc:  # noqa: BLE001 - report every failure as text
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(report)


class ScriptUploadWorker(_Worker):
    """Runs the HX Edit AppleScript uploader off the UI thread."""

    succeeded = Signal()
    failed = Signal(str)

    def __init__(
        self,
        preset_path: Path,
        *,
        mode: str = "auto",
        script_path: Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._preset_path = preset_path
        self._mode = mode
        self._script_path = script_path

    def run(self) -> None:
        try:
            upload_preset(self._preset_path, script_path=self._script_path, mode=self._mode)
        except Exception as exc:  # noqa: BLE001 - external automation, report don't crash
            self.failed.emit(str(exc))
            return
        self.succeeded.emit()
