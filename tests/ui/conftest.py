import os

# Must be set before PySide6/Qt is imported anywhere, so the test suite runs
# headless (no real display needed) both here and in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Imported after the platform setting above, deliberately.
import pytest


@pytest.fixture(autouse=True)
def hx_edit_installed(tmp_path_factory, monkeypatch):
    """Pretend HX Edit is installed, whatever the machine running the tests has.

    The upload controls are gated on finding it, so without this the suite
    would pass on a developer's Mac and fail on a runner, or the other way
    round. Tests about the missing case override this explicitly.
    """
    from hlxgen.device import hxedit

    bundle = tmp_path_factory.mktemp("hxedit") / "HX Edit.app"
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "Resources" / "Helix.sym").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(hxedit, "find_hx_edit", lambda explicit=None: bundle)
    return bundle
