"""Entry point: ``python -m hlxgen_ui``."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .main_window import MainWindow

_STYLE_SHEET_PATH = Path(__file__).with_name("style.qss")


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    if _STYLE_SHEET_PATH.exists():
        app.setStyleSheet(_STYLE_SHEET_PATH.read_text(encoding="utf-8"))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
