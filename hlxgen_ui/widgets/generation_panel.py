"""Generation panel: a step-by-step progress log.

Driven by ``on_progress`` messages from :func:`hlxgen_ui.generation.generate_tone`
(itself fed by ``hlxgen.llm.generate_chain_from_prompt``'s new progress
callback) - each call to :meth:`GenerationPanel.add_step` appends and
highlights one more completed step, so a several-second, multi-round-trip LLM
generation visibly progresses instead of sitting behind a bare spinner.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ..style import card_layout_margins, make_card


class GenerationPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        make_card(self)

        title = QLabel("Generation")
        title.setObjectName("panelTitle")

        self._status_label = QLabel("Idle")
        self._status_label.setObjectName("generationStatus")

        self._empty_label = QLabel("Steps will appear here once generation starts.")
        self._empty_label.setObjectName("generationEmpty")

        self._steps = QListWidget()
        # A compact log: it is progress, not the output.
        self._steps.setMaximumHeight(130)
        # Long steps (a full output path) are shortened in the middle rather
        # than growing a horizontal scrollbar; the tooltip keeps the whole text.
        self._steps.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._steps.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._steps.hide()

        layout = QVBoxLayout(self)
        card_layout_margins(layout)
        layout.addWidget(title)
        layout.addWidget(self._status_label)
        layout.addWidget(self._empty_label)
        layout.addWidget(self._steps, stretch=1)

    def start(self) -> None:
        self._steps.clear()
        self._empty_label.hide()
        self._steps.show()
        self._status_label.setText("Generating...")
        self._status_label.setProperty("state", "running")
        self._refresh_style()

    def add_step(self, message: str) -> None:
        item = QListWidgetItem(message)
        item.setToolTip(message)
        self._steps.addItem(item)
        self._steps.scrollToBottom()

    def finish_success(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setProperty("state", "success")
        self._refresh_style()

    def finish_failure(self, message: str) -> None:
        self._status_label.setText(f"Failed: {message}")
        self._status_label.setProperty("state", "failure")
        self._refresh_style()

    def _refresh_style(self) -> None:
        style = self._status_label.style()
        style.unpolish(self._status_label)
        style.polish(self._status_label)
