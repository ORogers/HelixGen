# Installing HelixGen

## The app

1. Download `HelixGen-<version>.dmg` from [Releases][releases].
2. Open it and drag **HelixGen** to Applications.
3. The first time you run it: **right-click the app and choose Open**, then
   click **Open** in the dialog that appears.

[releases]: https://github.com/ORogers/HelixGen/releases

### Why macOS warns you

The dialog says HelixGen "cannot be opened because it is from an unidentified
developer", or that Apple "cannot check it for malicious software".

That is Gatekeeper telling you the app is not signed with an Apple Developer ID
certificate, which costs $99 a year. It is a statement about a subscription, not
about the app. Everything HelixGen is built from is in this repository, and the
`.dmg` is built in the open by [a GitHub Actions workflow][release-workflow] you
can read - with a `SHA256SUMS` file published beside it so you can check the
download is the file that workflow produced.

[release-workflow]: ../.github/workflows/release.yml

Right-clicking and choosing **Open** is the supported way to run it anyway; you
only need to do it once.

If the app was quarantined and will not open at all:

```bash
xattr -dr com.apple.quarantine /Applications/HelixGen.app
```

Signing and notarisation are on the list. When they land, the warning goes away.

## The command line

```bash
pipx install helixgen
```

That gives you `helixgen`. To talk to a pedal you also need the USB stack and
libusb:

```bash
brew install libusb
pipx install 'helixgen[usb]'
```

The `.dmg` bundles its own libusb, so the app needs neither step.

## From a clone

```bash
git clone https://github.com/ORogers/HelixGen.git
cd HelixGen
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

See [CONTRIBUTING.md](../CONTRIBUTING.md).

## HX Edit

Talking to the pedal needs [HX Edit][hxedit] installed - HelixGen reads Line 6's
own data files out of it, because they are not ours to distribute. See
[legal.md](legal.md).

[hxedit]: https://line6.com/software/

HelixGen finds it in `/Applications/Line6/` or `/Applications/`, and falls back
to asking Spotlight, so an install anywhere else is usually found too. If not,
point at it under **Settings › HX Edit › Locate…**, or set:

```bash
export HELIXGEN_HX_EDIT="/path/to/HX Edit.app"
```

Generating, validating and inspecting presets all work without HX Edit.
