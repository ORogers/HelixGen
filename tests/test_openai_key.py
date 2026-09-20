"""Where the OpenAI key comes from, and where it is kept."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helixgen import config, llm


@pytest.fixture(autouse=True)
def _real_lookup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """These tests are about the lookup itself, so they use the real one.

    The suite-wide fixture stubs it out so that no other test can reach the
    API by accident; here it has to run. Run from an empty directory, because
    the lookup walks up from the working directory looking for a .env - and
    this repository has one.
    """
    monkeypatch.undo()
    monkeypatch.setenv("HELIXGEN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


def test_no_key_anywhere_is_none() -> None:
    assert llm.openai_api_key() is None


def test_a_stored_key_is_found() -> None:
    llm.store_openai_api_key("sk-stored")

    assert llm.openai_api_key() == "sk-stored"


def test_the_environment_beats_a_stored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell can override the saved key for one run without editing anything."""
    llm.store_openai_api_key("sk-stored")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")

    assert llm.openai_api_key() == "sk-from-environment"


def test_a_dotenv_file_beats_a_stored_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n", encoding="utf-8")
    llm.store_openai_api_key("sk-stored")

    assert llm.openai_api_key() == "sk-from-dotenv"


def test_storing_none_forgets_the_key() -> None:
    llm.store_openai_api_key("sk-stored")
    llm.store_openai_api_key(None)

    assert llm.openai_api_key() is None
    assert llm.OPENAI_API_KEY_SETTING not in config.read_settings()


def test_whitespace_is_stripped_off_a_pasted_key() -> None:
    """A key copied out of a browser usually arrives with a newline on it."""
    llm.store_openai_api_key("  sk-pasted\n")

    assert llm.openai_api_key() == "sk-pasted"
    assert config.read_setting(llm.OPENAI_API_KEY_SETTING) == "sk-pasted"


def test_an_empty_key_is_not_stored() -> None:
    llm.store_openai_api_key("   ")

    assert llm.openai_api_key() is None


def test_storing_a_key_drops_the_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a key changed in a running app is ignored until it restarts."""
    monkeypatch.setattr(llm, "_OPENAI_CLIENT", object())

    llm.store_openai_api_key("sk-new")

    assert llm._OPENAI_CLIENT is None


def test_the_key_file_is_not_readable_by_anyone_else() -> None:
    llm.store_openai_api_key("sk-secret")

    assert config.config_path().stat().st_mode & 0o077 == 0


def test_a_missing_key_says_what_to_do_about_it() -> None:
    with pytest.raises(llm.LLMGenerationError) as excinfo:
        llm._ensure_openai_api_key()

    message = str(excinfo.value)
    assert "Settings" in message
    assert "OPENAI_API_KEY" in message
    assert "ollama" in message


def test_the_key_shares_the_config_file_with_everything_else() -> None:
    """One file, so there is one place to look and one place to delete."""
    config.write_setting("hx_edit_path", "/Applications/HX Edit.app")
    llm.store_openai_api_key("sk-stored")

    stored = json.loads(config.config_path().read_text(encoding="utf-8"))
    assert stored["hx_edit_path"] == "/Applications/HX Edit.app"
    assert stored[llm.OPENAI_API_KEY_SETTING] == "sk-stored"
