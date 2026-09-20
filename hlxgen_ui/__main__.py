"""Entry point: ``python -m hlxgen_ui``."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow

_STYLE_SHEET_PATH = Path(__file__).with_name("style.qss")
_ICONS_PATH = Path(__file__).with_name("icons")


def load_style_sheet() -> str:
    """The app style sheet, with its icon URLs pointing at the bundled icons.

    Qt resolves a relative ``url()`` against the working directory, not the
    sheet, so the icon folder is substituted in as an absolute path.
    """
    sheet = _STYLE_SHEET_PATH.read_text(encoding="utf-8")
    return sheet.replace("{icons}", _ICONS_PATH.resolve().as_posix())


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    if _STYLE_SHEET_PATH.exists():
        app.setStyleSheet(load_style_sheet())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
