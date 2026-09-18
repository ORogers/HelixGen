"""Preview panel: a signal chain drawn the way it is heard - left to right.

A chain is a path, not a list, so it reads as one: each block is a card, the
arrows between them show the order the signal takes, and a cab the device
fuses into the amp before it is marked as such rather than being silently
dropped or silently promoted to a block of its own.

Fed from two places, and the shape is identical either way: a preset that was
just generated, or a slot read off the pedal.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..device import ChainBlock
from ..style import card_layout_margins, make_card


class PreviewPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        make_card(self)
        self.setMinimumHeight(210)

        self._title = QLabel("Signal chain")
        self._title.setObjectName("panelTitle")

        self._subtitle = QLabel("")
        self._subtitle.setObjectName("appSubtitle")

        self._empty_label = QLabel(
            "Generate a tone, or pick a slot on the left, to see its chain here."
        )
        self._empty_label.setObjectName("previewEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._chain_host = QWidget()
        self._chain_layout = QHBoxLayout(self._chain_host)
        self._chain_layout.setContentsMargins(2, 2, 2, 8)
        self._chain_layout.setSpacing(0)
        self._chain_layout.addStretch(1)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._chain_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.hide()

        header = QVBoxLayout()
        header.setSpacing(1)
        header.addWidget(self._title)
        header.addWidget(self._subtitle)

        layout = QVBoxLayout(self)
        card_layout_margins(layout)
        layout.addLayout(header)
        layout.addWidget(self._empty_label, stretch=1)
        layout.addWidget(self._scroll, stretch=1)

    # -- states -------------------------------------------------------------

    def show_chain(self, title: str, blocks: list[ChainBlock], *, subtitle: str = "") -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle or f"{len(blocks)} block(s), in signal order")
        self._clear_chain()

        if not blocks:
            self._empty_label.setText("This slot is empty.")
            self._empty_label.show()
            self._scroll.hide()
            return

        for position, block in enumerate(blocks):
            if position:
                self._chain_layout.insertWidget(
                    self._chain_layout.count() - 1, _arrow(block.fused_cab)
                )
            self._chain_layout.insertWidget(self._chain_layout.count() - 1, _Chip(block))

        self._empty_label.hide()
        self._scroll.show()

    def show_message(self, title: str, message: str) -> None:
        """Loading, empty or failed - one place, one look."""
        self._title.setText(title)
        self._subtitle.setText("")
        self._clear_chain()
        self._empty_label.setText(message)
        self._empty_label.show()
        self._scroll.hide()

    def chain_labels(self) -> list[str]:
        """Block names currently drawn, in signal order."""
        return [chip.block.name for chip in self._chain_host.findChildren(_Chip)]

    def clear(self) -> None:
        self.show_message(
            "Signal chain",
            "Generate a tone, or pick a slot on the left, to see its chain here.",
        )

    def _clear_chain(self) -> None:
        """Drop the drawn chain now, not whenever Qt gets round to it.

        ``deleteLater`` alone is not enough: taking a widget out of a layout
        leaves it parented, so it keeps painting where it was and keeps
        answering ``findChildren``. Showing a second chain then stacked it on
        top of the first - a slot's chain with the previously selected slot's
        chain still tacked on the front. Unparenting is what actually removes
        it; ``deleteLater`` then frees it safely.
        """
        while self._chain_layout.count() > 1:
            item = self._chain_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


class _Chip(QWidget):
    """One block: what it is, and what kind of thing it is."""

    def __init__(self, block: ChainBlock, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.block = block
        self.setObjectName("chip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("state", "bypassed" if not block.enabled else "on")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        name = QLabel(block.name)
        name.setObjectName("chipName")
        name.setWordWrap(True)
        name.setMaximumWidth(150)

        detail = block.category or "block"
        if block.fused_cab:
            detail = f"{detail} - in the amp"
        if not block.enabled:
            detail = f"{detail} - bypassed"
        caption = QLabel(detail)
        caption.setObjectName("chipCaption")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        layout.addWidget(name)
        layout.addWidget(caption)


def _arrow(fused: bool) -> QLabel:
    """``+`` for a cab riding inside its amp, ``→`` for a real hop."""
    arrow = QLabel("+" if fused else "→")
    arrow.setObjectName("chainArrow")
    arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
    arrow.setFixedWidth(26)
    return arrow
