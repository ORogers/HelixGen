"""The chain view replaces what it draws - it never accumulates.

Regression: the first version took chips out of the layout with
``deleteLater`` but left them parented, so a second chain rendered on top of
the first and a slot appeared to contain the previous slot's blocks too.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("pytestqt")

from hlxgen_ui.device import ChainBlock
from hlxgen_ui.widgets.preview_panel import PreviewPanel


def _blocks(*names: str) -> list[ChainBlock]:
    return [ChainBlock(position=i, name=n, category="Amp") for i, n in enumerate(names)]


def test_a_second_chain_replaces_the_first(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)

    panel.show_chain("Slot 1", _blocks("Scream 808", "Essex A30"))
    assert panel.chain_labels() == ["Scream 808", "Essex A30"]

    panel.show_chain("Slot 2", _blocks("Brit 2203"))
    assert panel.chain_labels() == ["Brit 2203"]


def test_showing_a_message_drops_the_chain(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)

    panel.show_chain("Slot 1", _blocks("Scream 808"))
    panel.show_message("Slot 2", "Reading from the pedal...")
    assert panel.chain_labels() == []


def test_an_empty_chain_reports_rather_than_keeping_the_last_one(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)

    panel.show_chain("Slot 1", _blocks("Scream 808"))
    panel.show_chain("Slot 2", [])
    assert panel.chain_labels() == []
    assert "empty" in panel._empty_label.text()


def test_a_bypassed_block_is_marked_not_hidden(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    panel.show_chain("Slot 1", [ChainBlock(position=0, name="Off Block", enabled=False)])
    assert panel.chain_labels() == ["Off Block"]
