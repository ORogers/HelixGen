# Packaging

## The macOS app

```bash
python3 -m pip install -e '.[ui]' pyinstaller
brew install libusb
pyinstaller packaging/HelixGen.spec --noconfirm
dist/HelixGen.app/Contents/MacOS/HelixGen --selftest
```

The self-test loads the catalog, schema and template out of the bundle and
checks libusb is reachable. A bundle missing one of those launches perfectly
well and then fails the first time someone asks it for anything, so the check
loads them rather than looking for the files.

[`.github/workflows/release.yml`](../.github/workflows/release.yml) does the
same thing on a `v*` tag, wraps the result in a `.dmg` with `hdiutil`, and
attaches it to the release with a `SHA256SUMS` file.

### Why PyInstaller and not py2app

`pyusb` loads libusb through `ctypes` at run time. PyInstaller can carry that
dylib into the bundle, which is what lets a downloaded app see a pedal without
`brew install libusb` first - most of the install friction the bundle exists to
remove.

### Why it is unsigned

Signing needs an Apple Developer ID certificate, which costs $99 a year.
Without one macOS shows an unidentified-developer warning the first time the
app is opened; [docs/install.md](../docs/install.md#why-macos-warns-you)
explains it to users. The release workflow has a TODO marking where signing and
notarisation go - it is two steps and three secrets, not a redesign.

### The icon

`packaging/HelixGen.icns`, if present, becomes the app icon; the spec falls back
to PyInstaller's default when it is missing. To make one from a 1024x1024 PNG:

```bash
mkdir HelixGen.iconset
for size in 16 32 64 128 256 512; do
  sips -z $size $size icon.png --out "HelixGen.iconset/icon_${size}x${size}.png"
  sips -z $((size*2)) $((size*2)) icon.png --out "HelixGen.iconset/icon_${size}x${size}@2x.png"
done
iconutil -c icns HelixGen.iconset -o packaging/HelixGen.icns
```
