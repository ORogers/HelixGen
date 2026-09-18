import os

# Must be set before PySide6/Qt is imported anywhere, so the test suite runs
# headless (no real display needed) both here and in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
