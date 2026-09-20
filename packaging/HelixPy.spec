# PyInstaller spec for the HelixPy desktop app.
#
# Built with:
#     pyinstaller packaging/HelixPy.spec --noconfirm
#
# PyInstaller rather than py2app for one reason: pyusb loads libusb through
# ctypes at run time, and PyInstaller can carry that dylib into the bundle.
# Without it every user would need `brew install libusb` before the app could
# see their pedal, which is most of the install friction this bundle exists to
# remove.

import ctypes.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

SPEC_DIR = Path(SPECPATH)
PROJECT_ROOT = SPEC_DIR.parent


def _libusb() -> list[tuple[str, str]]:
    """The libusb dylib, so the bundle does not need Homebrew.

    Located the same way pyusb locates it, so the bundled copy is the one that
    would have been loaded anyway. Missing it is not fatal at build time: the
    app still generates presets, it just cannot reach the pedal, and the build
    says so rather than failing.
    """
    found = ctypes.util.find_library("usb-1.0")
    if found is None:
        for candidate in ("/opt/homebrew/lib/libusb-1.0.0.dylib", "/usr/local/lib/libusb-1.0.0.dylib"):
            if Path(candidate).exists():
                found = candidate
                break
    if found is None:
        print("WARNING: libusb not found; the bundle will not be able to reach a device")
        return []
    print(f"Bundling libusb from {found}")
    return [(found, ".")]


a = Analysis(
    # launcher.py, not hlxgen_ui/__main__.py - see the note in that file.
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=_libusb(),
    # The model catalog, schema, template and AppleScript, plus the stylesheet
    # and icons. CI checks the built app still carries them.
    datas=[
        *collect_data_files("hlxgen", subdir="data"),
        (str(PROJECT_ROOT / "hlxgen_ui" / "style.qss"), "hlxgen_ui"),
        (str(PROJECT_ROOT / "hlxgen_ui" / "icons"), "hlxgen_ui/icons"),
    ],
    hiddenimports=[
        # Reached through function-local imports so that the plain CLI never
        # pulls in pyusb; PyInstaller cannot see those by static analysis.
        "hlxgen.device.commands",
        "hlxgen.device.editor",
        "hlxgen.device.usb",
        "usb.backend.libusb1",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt ships far more than this app uses, and every one of these pulls in
    # tens of megabytes.
    excludes=[
        "PySide6.Qt3DCore",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HelixPy",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HelixPy",
)

_icon = SPEC_DIR / "HelixPy.icns"

app = BUNDLE(
    coll,
    name="HelixPy.app",
    icon=str(_icon) if _icon.exists() else None,
    bundle_identifier="dev.orogers.helixpy",
    version="0.1.0",
    info_plist={
        "CFBundleName": "HelixPy",
        "CFBundleDisplayName": "HelixPy",
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Copyright 2025 Oliver Rogers. Apache-2.0.",
        # Without this macOS runs the app at 72 dpi and every panel renders
        # soft on a Retina display.
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
