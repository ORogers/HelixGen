"""Small shared helpers so every panel gets the same "card" treatment
consistently, instead of each widget file repeating the boilerplate.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QLayout, QWidget

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
