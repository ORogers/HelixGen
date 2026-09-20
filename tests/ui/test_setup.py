"""First-run setup, and settings that survive a restart."""

from __future__ import annotations

import pytest

from hlxgen import config, llm
from hlxgen_ui.main_window import MainWindow
from hlxgen_ui.settings import Settings


@pytest.fixture(autouse=True)
def _no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key anywhere, whatever the machine running the tests has."""
    monkeypatch.setattr("hlxgen_ui.main_window.openai_api_key", lambda: None)
    monkeypatch.setattr("hlxgen_ui.widgets.settings_page.openai_api_key", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_setup_is_shown_on_a_first_run(qtbot):
    """OpenAI is the default backend and it needs a key, which an app launched
    from the Dock has no environment to inherit."""
    window = MainWindow()
    qtbot.addWidget(window)

    assert window._pages.currentWidget() is window.setup_page
    assert window._settings_button.text() == "Skip"


def test_choosing_ollama_is_remembered_and_setup_does_not_return(qtbot):
    """Someone who deliberately declined a key must not be asked again."""
    window = MainWindow()
    qtbot.addWidget(window)

    window.setup_page._skip.click()

    assert window._pages.currentWidget() is not window.setup_page
    assert window._settings.backend == "ollama"

    again = MainWindow()
    qtbot.addWidget(again)
    assert again._settings.backend == "ollama"
    assert again._pages.currentWidget() is not again.setup_page


def test_setup_is_skipped_when_a_key_is_already_set(qtbot, monkeypatch):
    monkeypatch.setattr("hlxgen_ui.main_window.openai_api_key", lambda: "sk-test")

    window = MainWindow()
    qtbot.addWidget(window)

    assert window._pages.currentWidget() is not window.setup_page


def test_generating_without_a_key_offers_setup_rather_than_failing(qtbot):
    """The round trip would only come back with 'no key'. Ask for one instead."""
    window = MainWindow()
    qtbot.addWidget(window)
    window._show_workspace()

    window.prompt_panel._prompt_edit.setPlainText("warm bluesy crunch")
    window._on_generate()

    assert window._pages.currentWidget() is window.setup_page
    assert window._generation_worker is None
    # The prompt is still there to come back to.
    assert window.prompt_panel.prompt_text() == "warm bluesy crunch"


def test_a_saved_key_completes_setup(qtbot, monkeypatch):
    stored: list[str | None] = []
    monkeypatch.setattr(llm, "store_openai_api_key", stored.append)
    monkeypatch.setattr(
        "hlxgen_ui.widgets.setup_page.store_openai_api_key", stored.append
    )
    monkeypatch.setattr(
        "hlxgen_ui.workers.verify_openai_api_key", lambda key: None
    )

    window = MainWindow()
    qtbot.addWidget(window)
    window.setup_page._key.setText("sk-typed")
    window.setup_page._save.setEnabled(True)
    window.setup_page._save.click()

    qtbot.waitUntil(
        lambda: window._pages.currentWidget() is not window.setup_page, timeout=3000
    )
    assert stored == ["sk-typed"]
    assert window._settings.backend == "openai"


def test_a_rejected_key_is_kept_and_explained(qtbot, monkeypatch):
    """Saved before it is checked, so a key that works but cannot be verified
    right now - no network, say - is not thrown away."""
    monkeypatch.setattr(
        "hlxgen_ui.workers.verify_openai_api_key",
        lambda key: (_ for _ in ()).throw(llm.LLMGenerationError("That key was rejected.")),
    )

    window = MainWindow()
    qtbot.addWidget(window)
    page = window.setup_page
    page._key.setText("sk-wrong")
    page._save.setEnabled(True)
    page._save.click()

    qtbot.waitUntil(lambda: "rejected" in page._status.text(), timeout=3000)
    assert window._pages.currentWidget() is page, "a bad key must not close setup"
    # Read from the config rather than through openai_api_key, which the
    # suite-wide guard stubs out so no test can reach the real API.
    assert config.read_setting(llm.OPENAI_API_KEY_SETTING) == "sk-wrong"
    assert page._save.isEnabled(), "the user has to be able to try again"


def test_settings_survive_a_restart(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings_page._slot_count.setValue(48)
    window.settings_page._openai_model.setCurrentIndex(0)

    assert Settings.load().slot_count == 48
    assert Settings.load().openai_model == window._settings.openai_model
