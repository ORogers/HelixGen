"""Finding HX Edit, which every device operation depends on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlxgen import config
from hlxgen.device import hxedit


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test off the real config file and the real environment."""
    monkeypatch.setenv("HLXGEN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv(hxedit.HX_EDIT_ENV_VAR, raising=False)
    # No test may depend on whether the machine running it has HX Edit.
    monkeypatch.setattr(hxedit, "KNOWN_BUNDLE_PATHS", ())
    monkeypatch.setattr(hxedit, "_from_spotlight", lambda: None)
    return tmp_path


def make_bundle(root: Path, name: str = "HX Edit.app", *, amps: bool = True) -> Path:
    """A directory shaped like the parts of HX Edit that are read."""
    resources = root / name / "Contents" / "Resources"
    resources.mkdir(parents=True)
    (resources / "Helix.sym").write_text("[]", encoding="utf-8")
    if amps:
        (resources / "amp.models").write_text("[]", encoding="utf-8")
    return root / name


def test_explicit_path_wins_and_is_not_remembered(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)

    assert hxedit.find_hx_edit(bundle) == bundle
    # A one-off --hx-edit must not silently become the default for later runs.
    assert config.read_setting("hx_edit_path") is None


def test_explicit_path_that_is_not_hx_edit_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "Not HX Edit.app"
    empty.mkdir()

    assert hxedit.find_hx_edit(empty) is None


def test_environment_variable_overrides_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path, "Elsewhere.app")
    monkeypatch.setenv(hxedit.HX_EDIT_ENV_VAR, str(bundle))

    assert hxedit.find_hx_edit() == bundle


def test_a_known_location_is_found_and_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path)
    monkeypatch.setattr(hxedit, "KNOWN_BUNDLE_PATHS", (bundle,))

    assert hxedit.find_hx_edit() == bundle
    assert config.read_setting("hx_edit_path") == str(bundle)


def test_spotlight_finds_an_install_the_known_paths_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case this whole module exists for: HX Edit somewhere else entirely."""
    bundle = make_bundle(tmp_path / "Volumes" / "Audio", "HX Edit.app")
    monkeypatch.setattr(hxedit, "_from_spotlight", lambda: bundle)

    assert hxedit.find_hx_edit() == bundle
    assert config.read_setting("hx_edit_path") == str(bundle)


def test_a_remembered_path_skips_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = make_bundle(tmp_path)
    hxedit.remember_hx_edit(bundle)

    def fail() -> None:
        raise AssertionError("Spotlight was consulted despite a remembered path")

    monkeypatch.setattr(hxedit, "_from_spotlight", fail)
    assert hxedit.find_hx_edit() == bundle


def test_a_stale_remembered_path_is_discarded_and_rediscovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HX Edit moved. The old path must not pin the app to a dead install."""
    hxedit.remember_hx_edit(tmp_path / "gone" / "HX Edit.app")
    moved = make_bundle(tmp_path / "Applications")
    monkeypatch.setattr(hxedit, "_from_spotlight", lambda: moved)

    assert hxedit.find_hx_edit() == moved
    assert config.read_setting("hx_edit_path") == str(moved)


def test_a_bundle_without_the_symbol_table_is_not_hx_edit(tmp_path: Path) -> None:
    """Identified by the file that is actually read, not by its name.

    A half-installed or renamed copy is rejected here, rather than failing
    later with a confusing error about a missing symbol table.
    """
    hollow = tmp_path / "HX Edit.app" / "Contents" / "Resources"
    hollow.mkdir(parents=True)

    assert hxedit.find_hx_edit() is None


def test_nothing_found_returns_none_rather_than_raising() -> None:
    assert hxedit.find_hx_edit() is None
    assert hxedit.symbol_table_path() is None
    assert hxedit.amp_models_path() is None


def test_find_symbol_table_explains_what_to_do_when_there_is_no_install() -> None:
    with pytest.raises(hxedit.HxEditNotFound) as excinfo:
        hxedit.find_symbol_table()

    message = str(excinfo.value)
    assert "HX Edit" in message
    assert hxedit.HX_EDIT_ENV_VAR in message
    assert "--symbols" in message


def test_find_symbol_table_accepts_an_explicit_file(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    symbols = bundle / "Contents" / "Resources" / "Helix.sym"

    assert hxedit.find_symbol_table(symbols) == symbols
    with pytest.raises(FileNotFoundError):
        hxedit.find_symbol_table(tmp_path / "nope.sym")


def test_amp_models_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An install missing amp.models still supports uploading."""
    bundle = make_bundle(tmp_path, amps=False)
    monkeypatch.setattr(hxedit, "KNOWN_BUNDLE_PATHS", (bundle,))

    assert hxedit.symbol_table_path() is not None
    assert hxedit.amp_models_path() is None


def test_a_corrupt_config_file_is_treated_as_empty(tmp_path: Path) -> None:
    """A bad config must cost a rediscovery, never a crash on startup."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert config.read_setting("hx_edit_path") is None

    config.write_setting("hx_edit_path", "/somewhere")
    assert json.loads(path.read_text(encoding="utf-8")) == {"hx_edit_path": "/somewhere"}
