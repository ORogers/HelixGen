# Contributing

Thanks for looking. Bug reports, fixes and hardware reports are all welcome -
especially from anyone with a Helix device that is not an HX Stomp, since that
is the only unit this has been verified on.

## Getting set up

Python 3.13 or newer.

```bash
git clone https://github.com/ORogers/HelixPy.git
cd HelixGen
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

That installs the CLI, the desktop app, the USB stack and the test tooling.
`pip install -e .` alone gives you just the CLI, which is enough for anything
that does not touch a device or the UI.

`pyusb` loads libusb at run time: `brew install libusb`.

To run anything against a pedal you also need HX Edit installed - it carries
`Helix.sym`, which is Line 6's to distribute and not ours. See
[docs/legal.md](docs/legal.md).

## Before you open a PR

```bash
python3 -m pytest -q
python3 -m ruff check helixgen/ helixgen_ui/ tests/
```

Both run in CI on every pull request, along with a round trip that generates
and validates every chain in `docs/examples/` and a check that the built wheel
still carries its data files.

The UI tests need a Qt platform plugin and run headless; `tests/ui/conftest.py`
sets `QT_QPA_PLATFORM=offscreen` for you.

## House rules

**Never commit Line 6 data.** `Helix.sym`, `amp.models`, `HelixModelDefs.bin`
and their siblings are read from the user's own HX Edit install and are all
gitignored. Nor anything pulled off a pedal: a `.msgpack` backup is someone's
own presets.

**Firmware, flash and DFU are out of scope.** See
[docs/usb_protocol.md](docs/usb_protocol.md) §7 for the rest of the
hardware safety rules, and hold to them - they were learned by wedging a unit.

**Keep `docs/application_flow.md` current.** It is a trace of how the
application actually works, and it is only worth having if it is true. If your
change alters a path it describes, update it in the same PR.

**Bump `ruff.toml` and the CI pin together.** Both the version and the rule set
are pinned deliberately, so that a ruff release cannot fail the build on its
own.

## Style

Match the code around you. This codebase leans on comments and docstrings that
explain *why* a thing is the way it is - particularly in `helixgen/device/`,
where the answer is usually "because the hardware does something surprising".
A comment that records what the device actually did is worth more than one that
restates the code.

## Testing without hardware

Most of it can be. The device tests drive the codec, the framing and the
document parsing against fixtures, so the suite needs no pedal and no
proprietary data. A PR you could not test on hardware is still welcome - say so
in the description and it will be checked against a real unit before merging.
