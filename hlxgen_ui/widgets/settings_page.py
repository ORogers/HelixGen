"""The settings page: everything the workspace no longer has to show.

Edits are written straight back into the shared :class:`Settings` object as
the user changes them, so leaving the page needs no save step and nothing can
be half-applied.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..settings import BACKENDS, Settings
from ..style import card_layout_margins, make_card


class SettingsPage(QWidget):
    """A full page, not a dialog - it replaces the workspace while open."""

    closed = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

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

        self._ollama_model = QLineEdit(settings.ollama_model)
        self._ollama_model.textChanged.connect(self._apply)
        self._ollama_endpoint = QLineEdit(settings.ollama_endpoint)
        self._ollama_endpoint.textChanged.connect(self._apply)
        self._openai_model = QLineEdit(settings.openai_model)
        self._openai_model.textChanged.connect(self._apply)

        ollama_form = QWidget()
        ollama_layout = QFormLayout(ollama_form)
        ollama_layout.setContentsMargins(0, 0, 0, 0)
        ollama_layout.setSpacing(8)
        ollama_layout.addRow("Model", self._ollama_model)
        ollama_layout.addRow("Endpoint", self._ollama_endpoint)

        openai_form = QWidget()
        openai_layout = QFormLayout(openai_form)
        openai_layout.setContentsMargins(0, 0, 0, 0)
        openai_layout.setSpacing(8)
        openai_layout.addRow("Model", self._openai_model)

        self._backend_options = QStackedWidget()
        self._backend_options.addWidget(ollama_form)
        self._backend_options.addWidget(openai_form)
        self._backend_options.setCurrentIndex(self._backend_combo.currentIndex())

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

    def _on_backend_changed(self, index: int) -> None:
        self._backend_options.setCurrentIndex(index)
        self._apply()

    def _apply(self) -> None:
        """Push the form straight into the shared settings object."""
        self._settings.backend = self._backend_combo.currentData()
        self._settings.ollama_model = self._ollama_model.text().strip()
        self._settings.ollama_endpoint = self._ollama_endpoint.text().strip()
        self._settings.openai_model = self._openai_model.text().strip()
        self._settings.slot_count = self._slot_count.value()
