# Packaging

## Cutting a release

1. **Bump the version** in `pyproject.toml`. That is the only place it is
   written: the wheel, `helixgen --version` and the `.app`'s `Info.plist` all
   read it back from the installed metadata.
2. **Merge to `main`.** The workflow runs from the tagged commit, so the tag
   has to point at a commit that has it.
3. **Tag and push:**

   ```bash
   git checkout main && git pull
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

4. The workflow checks the tag matches `pyproject.toml`, builds the wheel, the
   sdist and `HelixGen.app`, runs the bundle's self-test, wraps it in a `.dmg`,
   and attaches everything with a `SHA256SUMS` file to a GitHub release whose
   notes are generated from the merged PRs.

To rehearse without publishing, run the workflow manually from the Actions tab:
it builds and checks everything and skips the publish step.

A release from a private repository is visible only to people who can see the
repository. Make it public first if the `.dmg` is meant to be downloadable.

### If something goes wrong

Delete the tag and the draft release, fix, and tag again:

```bash
git push origin :refs/tags/v0.2.0
gh release delete v0.2.0 --yes
```

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

`packaging/HelixGen.icns` is the app icon, and the spec falls back to
PyInstaller's default if it is ever missing. It is not a painted file: the mark
is drawn by [`icon/make_icon.py`](icon/make_icon.py) and every size in the
`.icns` is rendered from the vector rather than downsampled from one big
raster, so the small ones stay sharp.

```bash
python3 packaging/icon/make_icon.py --icns --out packaging/HelixGen.icns \
    --svg packaging/icon/helixgen.svg
```

Other concepts are kept in that file - `--concept helix-woven`, `pick-helix`,
`pick` - and any of them can be rendered on its own to look at:

```bash
python3 packaging/icon/make_icon.py --concept pick --out /tmp/pick.png --size 512
```

The size that decides an icon is 16px, which is what the Finder list view uses.
Render the ladder and look at it before changing the mark.
