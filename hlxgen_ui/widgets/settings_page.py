"""The settings page: everything the workspace no longer has to show.

Edits are written straight back into the shared :class:`Settings` object as
the user changes them, so leaving the page needs no save step and nothing can
be half-applied.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hlxgen.llm import (
    OPENAI_MODELS,
    REASONING_EFFORTS,
    nearest_reasoning_effort,
    supported_reasoning_efforts,
)

from ..settings import BACKENDS, Settings
from ..style import card_layout_margins, make_card
from ..workers import ModelListWorker

#: How each thinking level reads in the UI.
THINKING_LABELS = {
    "none": "None",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}

_THINKING_HINT = "Lower is faster. Higher reasons longer before answering."


class SettingsPage(QWidget):
    """A full page, not a dialog - it replaces the workspace while open."""

    closed = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._model_worker: ModelListWorker | None = None
        self._models_listed = False
        #: Thinking levels per installed Ollama model, as the server reported them.
        self._ollama_levels: dict[str, tuple[str, ...]] = {}

        card = QWidget()
        make_card(card)
        card.setMaximumWidth(680)

        title = QLabel("Settings")
        title.setObjectName("panelTitle")

        subtitle = QLabel("Applies to the next generation. Nothing is saved between runs yet.")
        subtitle.setObjectName("appSubtitle")
        subtitle.setWordWrap(True)

        generation_caption = QLabel("GENERATION")
        generation_caption.setObjectName("sectionLabel")

        self._backend_combo = QComboBox()
        for label, value in BACKENDS:
            self._backend_combo.addItem(label, value)
        self._backend_combo.setCurrentIndex(
            max(0, [value for _, value in BACKENDS].index(settings.backend))
        )
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)

        # -- Ollama ------------------------------------------------------
        # Editable, so any model name works even before (or without) the list
        # of installed models arriving from the server.
        self._ollama_model = QComboBox()
        self._ollama_model.setEditable(True)
        self._ollama_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._ollama_model.setEditText(settings.ollama_model)
        self._ollama_model.editTextChanged.connect(self._on_model_changed)

        self._refresh_models = QPushButton("Refresh")
        self._refresh_models.setToolTip("List the models installed on the Ollama server")
        self._refresh_models.clicked.connect(self._list_ollama_models)

        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(self._ollama_model, stretch=1)
        model_row.addWidget(self._refresh_models)

        self._models_status = QLabel("")
        self._models_status.setObjectName("fieldHint")
        self._models_status.setWordWrap(True)
        self._models_status.hide()

        self._ollama_endpoint = QLineEdit(settings.ollama_endpoint)
        self._ollama_endpoint.textChanged.connect(self._apply)
        self._ollama_endpoint.editingFinished.connect(self._on_endpoint_edited)

        self._num_ctx = QSpinBox()
        self._num_ctx.setRange(4096, 131072)
        self._num_ctx.setSingleStep(4096)
        self._num_ctx.setSuffix(" tokens")
        self._num_ctx.setValue(settings.num_ctx)
        self._num_ctx.setToolTip(
            "How much text the model can take in at once. It grows on its own if a "
            "prompt would not fit, so this rarely needs changing."
        )
        self._num_ctx.valueChanged.connect(self._apply)

        ollama_form = QWidget()
        ollama_layout = QFormLayout(ollama_form)
        ollama_layout.setContentsMargins(0, 0, 0, 0)
        ollama_layout.setSpacing(8)
        ollama_layout.addRow("Model", model_row)
        ollama_layout.addRow("", self._models_status)
        ollama_layout.addRow("Endpoint", self._ollama_endpoint)
        ollama_layout.addRow("Context window", self._num_ctx)

        # -- OpenAI --------------------------------------------------------
        self._openai_model = QComboBox()
        for model_id, description in OPENAI_MODELS:
            self._openai_model.addItem(f"{model_id}  -  {description}", model_id)
        self._openai_model.setCurrentIndex(
            max(0, self._openai_model.findData(settings.openai_model))
        )
        self._openai_model.currentIndexChanged.connect(self._on_model_changed)

        openai_form = QWidget()
        openai_layout = QFormLayout(openai_form)
        openai_layout.setContentsMargins(0, 0, 0, 0)
        openai_layout.setSpacing(8)
        openai_layout.addRow("Model", self._openai_model)

        self._backend_options = QStackedWidget()
        self._backend_options.addWidget(ollama_form)
        self._backend_options.addWidget(openai_form)
        self._show_backend_options(self._backend_combo.currentIndex())

        # -- Thinking level (both backends) --------------------------------
        self._thinking = QComboBox()
        for level in REASONING_EFFORTS:
            self._thinking.addItem(THINKING_LABELS[level], level)
        self._thinking.setCurrentIndex(max(0, self._thinking.findData(settings.reasoning_effort)))
        self._thinking.currentIndexChanged.connect(self._on_thinking_changed)

        self._thinking_hint = QLabel(_THINKING_HINT)
        self._thinking_hint.setObjectName("fieldHint")
        self._thinking_hint.setWordWrap(True)
        # Room for two lines whatever the hint says, so the page below doesn't
        # jump as the hint changes.
        self._thinking_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        thinking_form = QFormLayout()
        thinking_form.setSpacing(8)
        thinking_form.addRow("Thinking level", self._thinking)
        thinking_form.addRow("", self._thinking_hint)

        device_caption = QLabel("DEVICE")
        device_caption.setObjectName("sectionLabel")

        self._slot_count = QSpinBox()
        self._slot_count.setRange(1, 126)
        self._slot_count.setValue(settings.slot_count)
        self._slot_count.setToolTip(
            "Slots a Refresh reads. Each one costs a document read off the pedal, "
            "so a bigger sweep shows more and takes longer."
        )
        self._slot_count.valueChanged.connect(self._apply)

        backend_form = QFormLayout()
        backend_form.setSpacing(8)
        backend_form.addRow("Backend", self._backend_combo)

        device_form = QFormLayout()
        device_form.setSpacing(8)
        device_form.addRow("Slots to read", self._slot_count)

        # Every form shares one label column width, so the fields line up
        # across the backend, model and thinking rows.
        label_width = max(
            form.labelForField(field).sizeHint().width()
            for form, field in (
                (ollama_layout, self._num_ctx),
                (thinking_form, self._thinking),
                (backend_form, self._backend_combo),
                (device_form, self._slot_count),
            )
        )
        self._forms = (ollama_layout, openai_layout, thinking_form, backend_form, device_form)
        for form in self._forms:
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if item is not None and item.widget() is not None:
                    item.widget().setMinimumWidth(label_width)

        done = QPushButton("Done")
        done.setObjectName("primaryButton")
        done.setMinimumHeight(36)
        done.clicked.connect(self.closed)

        done_row = QHBoxLayout()
        done_row.addStretch(1)
        done_row.addWidget(done)

        card_layout = QVBoxLayout(card)
        card_layout_margins(card_layout)
        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(8)
        card_layout.addWidget(generation_caption)
        card_layout.addLayout(backend_form)
        card_layout.addWidget(self._backend_options)
        card_layout.addLayout(thinking_form)
        card_layout.addSpacing(12)
        card_layout.addWidget(device_caption)
        card_layout.addLayout(device_form)
        card_layout.addSpacing(12)
        card_layout.addLayout(done_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        self._update_thinking_levels()

    # -- events ---------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_to_style()
        # Ask the server once, the first time the page is actually opened, so
        # starting the app never waits on (or needs) an Ollama server.
        if not self._models_listed:
            self._list_ollama_models()

    def _show_backend_options(self, index: int) -> None:
        """Show one backend's fields, sized to that backend alone.

        A stacked widget is as tall as its tallest page, which left a gap the
        height of the Ollama fields under OpenAI's single row.
        """
        self._backend_options.setCurrentIndex(index)
        for page in range(self._backend_options.count()):
            policy = (
                QSizePolicy.Policy.Preferred if page == index else QSizePolicy.Policy.Ignored
            )
            self._backend_options.widget(page).setSizePolicy(policy, policy)
        self._backend_options.adjustSize()

    def _fit_to_style(self) -> None:
        """Sizes that depend on the style sheet, which only applies once shown.

        Each label is made as tall as its field so it centres on it rather than
        sitting on its top edge, and the thinking hint keeps room for two lines
        whatever it says, so the page below it doesn't jump as it changes.
        """
        for form in self._forms:
            for row in range(form.rowCount()):
                label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                if label is None or label.widget() is None or field is None:
                    continue
                label.widget().setAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                label.widget().setMinimumHeight(field.sizeHint().height())
        self._thinking_hint.setFixedHeight(self._thinking_hint.fontMetrics().lineSpacing() * 2)

    def _on_backend_changed(self, index: int) -> None:
        self._show_backend_options(index)
        self._apply()
        self._update_thinking_levels()

    def _on_model_changed(self, *_: object) -> None:
        self._apply()
        self._update_thinking_levels()

    def _on_endpoint_edited(self) -> None:
        if self._models_listed:
            self._list_ollama_models()

    def _on_thinking_changed(self, _index: int) -> None:
        self._apply()
        self._thinking_hint.setText(_THINKING_HINT)

    # -- Ollama model list ---------------------------------------------------

    def _list_ollama_models(self) -> None:
        if self._model_worker is not None and self._model_worker.isRunning():
            return
        self._models_listed = True
        self._refresh_models.setEnabled(False)
        self._model_worker = ModelListWorker(self._settings.ollama_endpoint)
        self._model_worker.succeeded.connect(self._on_models_listed)
        self._model_worker.failed.connect(self._on_models_failed)
        self._model_worker.finished.connect(lambda: self._refresh_models.setEnabled(True))
        self._model_worker.start()

    def _on_models_listed(self, models: list[tuple[str, tuple[str, ...]]]) -> None:
        self._ollama_levels = dict(models)
        current = self._ollama_model.currentText()
        self._ollama_model.blockSignals(True)
        self._ollama_model.clear()
        for name, _levels in models:
            self._ollama_model.addItem(name)
        self._ollama_model.setEditText(current)
        self._ollama_model.blockSignals(False)
        if models:
            self._models_status.hide()
        else:
            self._show_models_status("No models are installed on this Ollama server.")
        self._update_thinking_levels()

    def _on_models_failed(self, message: str) -> None:
        self._show_models_status(f"Couldn't list installed models. {message}")

    def _show_models_status(self, message: str) -> None:
        self._models_status.setText(message)
        self._models_status.show()

    # -- thinking level ---------------------------------------------------------

    def _current_model(self) -> str:
        if self._settings.backend == "openai":
            return self._settings.openai_model
        return self._settings.ollama_model

    def _levels_for_current_model(self) -> tuple[str, ...]:
        model = self._current_model()
        if self._settings.backend == "ollama" and model in self._ollama_levels:
            return self._ollama_levels[model]
        return supported_reasoning_efforts(self._settings.backend, model)

    def _update_thinking_levels(self) -> None:
        """Grey out the levels the chosen model can't use, moving the selection
        to the nearest one it can if the current level is among them."""

        levels = self._levels_for_current_model()
        model_view: QStandardItemModel = self._thinking.model()
        for row, level in enumerate(REASONING_EFFORTS):
            item = model_view.item(row)
            enabled = level in levels
            item.setEnabled(enabled)
            item.setToolTip("" if enabled else f"{self._current_model()} doesn't offer this level")

        if not levels:
            self._thinking.setEnabled(False)
            self._thinking_hint.setText(
                f"{self._current_model() or 'This model'} doesn't think, "
                "so the thinking level doesn't apply."
            )
            return

        self._thinking.setEnabled(True)
        wanted = self._settings.reasoning_effort
        if wanted in levels:
            self._thinking_hint.setText(_THINKING_HINT)
            return

        nearest = nearest_reasoning_effort(wanted, levels)
        self._thinking.blockSignals(True)
        self._thinking.setCurrentIndex(self._thinking.findData(nearest))
        self._thinking.blockSignals(False)
        self._apply()
        self._thinking_hint.setText(
            f"Set to {THINKING_LABELS[nearest]}: {self._current_model()} "
            f"doesn't offer {THINKING_LABELS[wanted]}."
        )

    # -- settings -----------------------------------------------------------------

    def _apply(self) -> None:
        """Push the form straight into the shared settings object."""
        self._settings.backend = self._backend_combo.currentData()
        self._settings.ollama_model = self._ollama_model.currentText().strip()
        self._settings.ollama_endpoint = self._ollama_endpoint.text().strip()
        self._settings.num_ctx = self._num_ctx.value()
        self._settings.openai_model = self._openai_model.currentData()
        self._settings.reasoning_effort = self._thinking.currentData()
        self._settings.slot_count = self._slot_count.value()
