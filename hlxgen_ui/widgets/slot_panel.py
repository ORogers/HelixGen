"""Slot panel: see what is on the pedal and pick a target slot.

Rows come from :func:`hlxgen_ui.device.read_slots`, which reads each slot's
document *without loading it* - the pedal's panel does not move and an
uncommitted edit survives a refresh.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..device import DEFAULT_SLOT_COUNT, DeviceSummary, SlotSummary
from ..style import card_layout_margins, make_card

__all__ = ["DEFAULT_SLOT_COUNT", "SlotPanel"]


class SlotPanel(QWidget):
    """Left-hand panel: device identity, slot list, refresh, selection."""

    #: Emitted with the slot index whenever the user selects a row.
    slot_selected = Signal(int)
    #: Emitted when the user clicks Refresh - the main window owns starting
    #: the actual (threaded) read, this panel only asks for it.
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        make_card(self)
        self.setMinimumWidth(250)

        title = QLabel("Device")
        title.setObjectName("panelTitle")

        self._device_label = QLabel("Not connected")
        self._device_label.setObjectName("deviceName")
        self._device_label.setWordWrap(True)

        self._status_label = QLabel("Click Refresh to look for a pedal.")
        self._status_label.setObjectName("slotStatus")
        self._status_label.setWordWrap(True)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh_requested)

        layout = QVBoxLayout(self)
        card_layout_margins(layout)
        layout.addWidget(title)
        layout.addWidget(self._device_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._list, stretch=1)
        layout.addWidget(self._refresh_button)

    # -- state transitions ---------------------------------------------

    def set_scanning(self) -> None:
        self._status_label.setText("Looking for a device...")
        self._refresh_button.setEnabled(False)

    def set_device(self, device: DeviceSummary | None) -> None:
        if device is None:
            self._device_label.setText("Not connected")
            return
        suffix = "" if device.verified else "  (untested model)"
        self._device_label.setText(f"{device.description}{suffix}")

    def set_reading(self, message: str = "Reading slots...") -> None:
        self._status_label.setText(message)
        self._refresh_button.setEnabled(False)

    def set_no_device(self, reason: str = "No device connected") -> None:
        """Nothing usable attached: clear the rows *and* the identity line."""
        self._list.clear()
        self._device_label.setText("Not connected")
        self._status_label.setText(reason)
        self._refresh_button.setEnabled(True)

    def set_hint(self, message: str) -> None:
        """A device is attached; we just have nothing read from it yet.

        Deliberately separate from :meth:`set_no_device`, which would wipe the
        identity line we have just filled in.
        """
        self._status_label.setText(message)
        self._refresh_button.setEnabled(True)

    def set_slots(self, slots: list[SlotSummary]) -> None:
        self._list.clear()
        self._refresh_button.setEnabled(True)
        populated = sum(1 for slot in slots if slot.populated)
        self._status_label.setText(f"{populated} of {len(slots)} slots in use")
        for slot in slots:
            item = QListWidgetItem(slot.label())
            item.setData(Qt.ItemDataRole.UserRole, slot.index)
            if slot.populated and slot.detail and slot.name:
                item.setToolTip(slot.detail)
            if not slot.populated:
                item.setForeground(Qt.GlobalColor.gray)
            self._list.addItem(item)

    def selected_slot(self) -> int | None:
        items = self._list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self) -> None:
        slot_index = self.selected_slot()
        if slot_index is not None:
            self.slot_selected.emit(slot_index)
