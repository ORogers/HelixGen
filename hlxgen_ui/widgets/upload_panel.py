"""Upload panel: choose a transport, confirm the target, send the preset.

Two transports, matching what ``hlxgen`` itself offers:

* **USB** (default) - writes straight into a chosen slot, the same surgical
  ``apply_tone`` path ``hlxgen push`` uses. Needs a slot selected in the
  device panel, because it overwrites what is there.
* **HX Edit** - the AppleScript automation, macOS only, which cannot target a
  slot itself ('auto' always overwrites slot 1).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..style import card_layout_margins, make_card

TRANSPORT_USB = "usb"
TRANSPORT_SCRIPT = "script"


class UploadPanel(QWidget):
    #: (transport, mode) - mode is the AppleScript mode, ignored for USB.
    upload_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        make_card(self)
        self.setMinimumWidth(270)

        self._has_preset = False
        self._slot: int | None = None

        title = QLabel("Upload")
        title.setObjectName("panelTitle")

        preset_caption = QLabel("PRESET")
        preset_caption.setObjectName("sectionLabel")
        self._target_label = QLabel("Nothing generated yet")
        self._target_label.setObjectName("slotStatus")
        self._target_label.setWordWrap(True)

        target_caption = QLabel("TARGET SLOT")
        target_caption.setObjectName("sectionLabel")
        self._slot_label = QLabel("None selected")
        self._slot_label.setWordWrap(True)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)

        self._transport_combo = QComboBox()
        self._transport_combo.addItem("USB (direct to slot)", TRANSPORT_USB)
        self._transport_combo.addItem("HX Edit (AppleScript)", TRANSPORT_SCRIPT)
        self._transport_combo.currentIndexChanged.connect(self._sync_enabled)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Auto (overwrites slot 1)", "auto")
        self._mode_combo.addItem("Manual (pick in HX Edit)", "manual")

        self._mode_label = QLabel("HX Edit mode")

        self._auto_upload = QCheckBox("Upload automatically after generating")
        self._auto_upload.setChecked(True)
        self._auto_upload.setToolTip(
            "When a tone finishes generating, send it straight to the selected "
            "slot. This overwrites that slot without asking again."
        )

        self._upload_button = QPushButton("Upload")
        self._upload_button.setObjectName("primaryButton")
        self._upload_button.setEnabled(False)
        self._upload_button.setMinimumHeight(38)
        self._upload_button.clicked.connect(self._on_upload_clicked)

        self._status_label = QLabel("")
        self._status_label.setObjectName("uploadStatus")
        self._status_label.setWordWrap(True)

        form = QFormLayout()
        form.setSpacing(8)
        form.addRow("Transport", self._transport_combo)
        form.addRow(self._mode_label, self._mode_combo)

        layout = QVBoxLayout(self)
        card_layout_margins(layout)
        layout.addWidget(title)
        layout.addWidget(preset_caption)
        layout.addWidget(self._target_label)
        layout.addWidget(target_caption)
        layout.addWidget(self._slot_label)
        layout.addWidget(divider)
        layout.addLayout(form)
        layout.addWidget(self._auto_upload)
        layout.addSpacing(4)
        layout.addWidget(self._upload_button)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

        self._sync_enabled()

    # -- inputs ------------------------------------------------------------

    def transport(self) -> str:
        return self._transport_combo.currentData()

    def auto_upload_enabled(self) -> bool:
        return self._auto_upload.isChecked()

    def mode(self) -> str:
        return self._mode_combo.currentData()

    def set_generated_preset(self, output_path: Path | None) -> None:
        self._has_preset = output_path is not None
        self._target_label.setText(
            output_path.name if output_path is not None else "Nothing generated yet"
        )
        if output_path is not None:
            self._target_label.setToolTip(str(output_path))
        self._sync_enabled()

    def set_selected_slot(self, slot_index: int | None) -> None:
        self._slot = slot_index
        self._slot_label.setText(
            "None selected" if slot_index is None else f"Slot {slot_index}"
        )
        self._sync_enabled()

    # -- state -------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._transport_combo.setEnabled(not busy)
        self._mode_combo.setEnabled(not busy)
        self._auto_upload.setEnabled(not busy)
        if busy:
            self._upload_button.setEnabled(False)
        else:
            self._sync_enabled()

    def set_progress(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setProperty("state", "running")
        self._refresh_style()

    def set_success(self, message: str) -> None:
        self.set_busy(False)
        self._status_label.setText(message)
        self._status_label.setProperty("state", "success")
        self._refresh_style()

    def set_failure(self, message: str) -> None:
        self.set_busy(False)
        self._status_label.setText(f"Upload failed: {message}")
        self._status_label.setProperty("state", "failure")
        self._refresh_style()

    def _sync_enabled(self) -> None:
        """Keep the button and the hint honest about what is missing."""
        usb = self.transport() == TRANSPORT_USB
        self._mode_label.setVisible(not usb)
        self._mode_combo.setVisible(not usb)

        if not self._has_preset:
            self._upload_button.setEnabled(False)
            self._upload_button.setToolTip("Generate a tone first.")
            return
        if usb and self._slot is None:
            self._upload_button.setEnabled(False)
            self._upload_button.setToolTip(
                "Pick a target slot in the device panel - a USB upload "
                "overwrites the slot it is given."
            )
            return
        self._upload_button.setEnabled(True)
        self._upload_button.setToolTip(
            f"Overwrite slot {self._slot} over USB." if usb else "Hand the preset to HX Edit."
        )

    def _on_upload_clicked(self) -> None:
        self.upload_requested.emit(self.transport(), self._mode_combo.currentData())

    def _refresh_style(self) -> None:
        style = self._status_label.style()
        style.unpolish(self._status_label)
        style.polish(self._status_label)
