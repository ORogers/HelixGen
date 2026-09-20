"""Small shared helpers so every panel gets the same "card" treatment
consistently, instead of each widget file repeating the boilerplate.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QResizeEvent
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLabel, QLayout, QWidget

#: Standard spacing inside a card panel - generous enough that content
#: doesn't feel jammed against the rounded border drawn by QSS (#card in
#: style.qss), matching the "consistent rhythm across panels" UX guidance.
CARD_MARGINS = (18, 16, 18, 16)
CARD_SPACING = 10


def make_card(widget: QWidget) -> None:
    """Turn a plain QWidget into a styled "card" surface: rounded, bordered,
    with a soft drop shadow for depth. QSS alone can't paint a box-shadow, so
    the shadow is a real QGraphicsDropShadowEffect.
    """
    widget.setObjectName("card")
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(28)
    shadow.setOffset(0, 4)
    shadow.setColor(QColor(0, 0, 0, 110))
    widget.setGraphicsEffect(shadow)


def card_layout_margins(layout: QLayout) -> None:
    layout.setContentsMargins(*CARD_MARGINS)
    layout.setSpacing(CARD_SPACING)


class ElidedLabel(QLabel):
    """One line that shrinks from the middle instead of wrapping.

    A filesystem path wrapped across two lines breaks mid-word and pushes the
    row it sits in out of alignment with its neighbours. Eliding the middle
    keeps the ends -- the volume and the bundle name, which is what identifies
    an install -- and the full text stays in the tooltip.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._apply_elision()

    def fullText(self) -> str:
        """The unelided text, for tests and for anything that has to read it."""
        return self._full_text

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, max(self.width(), 0)
        )
        super().setText(elided)
