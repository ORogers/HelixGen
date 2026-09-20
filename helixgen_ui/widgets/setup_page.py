"""First-run setup: choose how tones get generated, and get a key in place.

HelixGen cannot generate anything without a backend behind it, and the better
one needs an API key. An app launched from the Dock has no shell environment to
inherit ``OPENAI_API_KEY`` from, so without this page the only way to set one up
would be a terminal - which defeats the point of shipping an app at all.

Shown once, when there is no key saved and no backend chosen. It is skippable:
Ollama needs no key, and someone who already runs it should not be made to read
about OpenAI first.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from helixgen.llm import store_openai_api_key

from ..style import card_layout_margins, make_card
from ..workers import ApiKeyTestWorker

API_KEYS_URL = "https://platform.openai.com/api-keys"

_INTRO = (
    "HelixGen turns a description of a tone into a preset. Something has to do "
    "the thinking, and there are two choices."
)

_OPENAI_PITCH = (
    "<b>OpenAI</b> - the recommended one. A tone takes seconds and the chains "
    "are better. You pay OpenAI per tone; it is a fraction of a penny each."
)

_OLLAMA_PITCH = (
    "<b>Ollama</b> - free, offline, and nothing leaves your machine. Slower, "
    "and how good the result is depends on the model you have pulled. You can "
    "switch at any time under Settings."
)

#: Qt's default link colour is a dark blue that all but disappears on this
#: background, and QSS cannot restyle an anchor inside a rich-text QLabel, so
#: the app's accent is set on the tag itself.
_LINK_COLOUR = "#9d86ff"

_STEPS = (
    "1. &nbsp;Open <a href='{url}' style='color: {colour}; text-decoration: none'>"
    "platform.openai.com/api-keys</a> and create a key.<br>"
    "2. &nbsp;Copy it - it is only shown once.<br>"
    "3. &nbsp;Paste it below."
)


class SetupPage(QWidget):
    """A full page, shown in place of the workspace until it is dealt with."""

    #: Emitted with the backend the user settled on, once setup is done.
    completed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key_worker: ApiKeyTestWorker | None = None

        card = QWidget()
        make_card(card)
        # Both bounds: the stretches either side would otherwise squeeze the
        # card down to the width of its longest unwrappable line, which for a
        # page that is mostly prose is far too narrow to read.
        card.setMinimumWidth(520)
        card.setMaximumWidth(620)

        title = QLabel("Welcome to HelixGen")
        title.setObjectName("panelTitle")

        intro = QLabel(_INTRO)
        intro.setWordWrap(True)

        openai_pitch = QLabel(_OPENAI_PITCH)
        openai_pitch.setWordWrap(True)

        ollama_pitch = QLabel(_OLLAMA_PITCH)
        ollama_pitch.setObjectName("fieldHint")
        ollama_pitch.setWordWrap(True)

        steps_caption = QLabel("GET AN OPENAI KEY")
        steps_caption.setObjectName("sectionLabel")

        steps = QLabel(_STEPS.format(url=API_KEYS_URL, colour=_LINK_COLOUR))
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setOpenExternalLinks(False)
        steps.linkActivated.connect(lambda url: QDesktopServices.openUrl(url))

        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("sk-...")
        self._key.setMinimumHeight(34)
        self._key.textEdited.connect(self._sync_enabled)
        self._key.returnPressed.connect(self._save_key)

        self._status = QLabel("")
        self._status.setObjectName("fieldHint")
        self._status.setWordWrap(True)

        self._save = QPushButton("Save and continue")
        self._save.setObjectName("primaryButton")
        self._save.setMinimumHeight(36)
        self._save.setEnabled(False)
        self._save.clicked.connect(self._save_key)

        self._skip = QPushButton("Use Ollama instead")
        self._skip.setMinimumHeight(36)
        self._skip.clicked.connect(lambda: self.completed.emit("ollama"))

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._skip)
        buttons.addStretch(1)
        buttons.addWidget(self._save)

        layout = QVBoxLayout(card)
        card_layout_margins(layout)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addSpacing(6)
        layout.addWidget(openai_pitch)
        layout.addWidget(ollama_pitch)
        layout.addSpacing(10)
        layout.addWidget(steps_caption)
        layout.addWidget(steps)
        layout.addWidget(self._key)
        layout.addWidget(self._status)
        layout.addSpacing(6)
        layout.addLayout(buttons)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    # -- internals ------------------------------------------------------

    def _sync_enabled(self, *_args: object) -> None:
        self._save.setEnabled(bool(self._key.text().strip()))

    def _save_key(self) -> None:
        """Store the key, then check it is accepted.

        Stored before it is checked so that a working key is not thrown away
        because the check could not run - no network, say. A rejected key stays
        stored too, and the message says why, which is more useful than being
        silently returned to an empty field.
        """
        key = self._key.text().strip()
        if not key or (self._key_worker is not None and self._key_worker.isRunning()):
            return

        store_openai_api_key(key)
        self._save.setEnabled(False)
        self._skip.setEnabled(False)
        self._show_status("Checking the key...", problem=False)

        self._key_worker = ApiKeyTestWorker(key, self)
        self._key_worker.succeeded.connect(self._on_accepted)
        self._key_worker.failed.connect(self._on_rejected)
        self._key_worker.finished.connect(lambda: setattr(self, "_key_worker", None))
        self._key_worker.start()

    def _on_accepted(self) -> None:
        self._key.clear()
        self._skip.setEnabled(True)
        self.completed.emit("openai")

    def _on_rejected(self, message: str) -> None:
        self._save.setEnabled(True)
        self._skip.setEnabled(True)
        self._show_status(
            f"{message} It has been saved anyway - you can fix it under Settings.",
            problem=True,
        )

    def _show_status(self, message: str, *, problem: bool) -> None:
        self._status.setText(message)
        self._status.setProperty("missing", problem)
        style = self._status.style()
        style.unpolish(self._status)
        style.polish(self._status)
