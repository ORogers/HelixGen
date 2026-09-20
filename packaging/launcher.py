"""Entry point for the frozen app.

``hlxgen_ui/__main__.py`` cannot be used directly: PyInstaller runs its entry
script as a top-level module, and that file's relative imports
(``from .main_window import ...``) need a parent package, so freezing it
straight produced a bundle that built cleanly and then died on launch with
"attempted relative import with no known parent package".

Importing the package here keeps one implementation of ``main`` rather than a
second copy that could drift from it.
"""

import sys


def selftest() -> int:
    """Check a built bundle carries what it needs, and can read it.

    A bundle missing a data file launches perfectly well and then fails the
    first time someone asks it for anything, so the check has to load them
    rather than just look for the files. Run by CI against the built app
    before the .dmg is published:

        HelixPy.app/Contents/MacOS/HelixPy --selftest
    """
    from hlxgen import resources
    from hlxgen.dataset import ModelCatalog
    from hlxgen.io import load_json_file

    catalog = ModelCatalog(resources.dataset_path())
    models = len(list(catalog.models()))
    if models == 0:
        print("FAIL: the model catalog is empty", file=sys.stderr)
        return 1

    for name, path in (
        ("schema", resources.schema_path()),
        ("template", resources.template_path()),
    ):
        if not load_json_file(path):
            print(f"FAIL: {name} at {path} is empty", file=sys.stderr)
            return 1

    if not resources.upload_script_path().exists():
        print("FAIL: the AppleScript uploader is missing", file=sys.stderr)
        return 1

    try:
        import usb.backend.libusb1

        backend = "found" if usb.backend.libusb1.get_backend() else "MISSING"
    except ImportError:
        backend = "pyusb not bundled"

    print(f"catalog: {models} models")
    print(f"output directory: {resources.default_output_dir()}")
    print(f"libusb: {backend}")
    if backend != "found":
        print("FAIL: this bundle could not reach libusb", file=sys.stderr)
        return 1
    return 0


def run() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    from hlxgen_ui.__main__ import main

    return main([arg for arg in sys.argv if arg != "--selftest"])


if __name__ == "__main__":
    sys.exit(run())
