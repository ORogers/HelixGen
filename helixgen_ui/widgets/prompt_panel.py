"""Prompt panel: the one field a person fills in on every run.

Backend and model settings deliberately do **not** live here - they are the
same on almost every run, so they sit on the settings page instead and this
panel stays down to a description and a button.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..style import card_layout_margins, make_card


class PromptPanel(QWidget):
    """Top-centre panel: tone prompt plus Generate."""

    #: Emitted when Generate is clicked. The prompt is already non-empty.
    generate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        make_card(self)

        title = QLabel("Describe the tone")
        title.setObjectName("panelTitle")

        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setPlaceholderText(
            'e.g. "warm, slightly overdriven blues tone with a touch of spring reverb"'
        )
        self._prompt_edit.setMinimumHeight(64)
        self._prompt_edit.setMaximumHeight(104)
        self._prompt_edit.textChanged.connect(self._update_generate_enabled)

        self._generate_button = QPushButton("Generate")
        self._generate_button.setObjectName("primaryButton")
        self._generate_button.setEnabled(False)
        self._generate_button.setMinimumHeight(38)
        self._generate_button.clicked.connect(self.generate_requested)

        layout = QVBoxLayout(self)
        card_layout_margins(layout)
        layout.addWidget(title)
        layout.addWidget(self._prompt_edit)
        layout.addWidget(self._generate_button)

    # -- reads -----------------------------------------------------------

    def prompt_text(self) -> str:
        return self._prompt_edit.toPlainText().strip()

    # -- state -------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """Disable input while a generation is in flight."""
        self._generate_button.setEnabled(not busy and bool(self.prompt_text()))
        self._generate_button.setText("Generating..." if busy else "Generate")
        self._prompt_edit.setEnabled(not busy)

    def _update_generate_enabled(self) -> None:
        self._generate_button.setEnabled(bool(self.prompt_text()))
