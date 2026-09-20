# HelixPy

[![CI](https://github.com/ORogers/HelixPy/actions/workflows/ci.yml/badge.svg)](https://github.com/ORogers/HelixPy/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Describe a guitar tone in plain English. Get it on your Line 6 HX Stomp.**

HelixPy turns "warm, dark jazz tone - hollowbody into a small valve amp, light
compression, a short room reverb" into a real Helix preset, validates it
against the full model catalog, and writes it straight into a slot on your
pedal over USB. There is a desktop app and a command-line tool; both do the
same thing.

![The HelixPy desktop app, showing a generated signal chain](docs/images/app.png)

> **This writes to your pedal.** Take a backup before the first write -
> `hlxgen backup --output ~/helix-backup` copies every slot and sends the pedal
> nothing. Firmware, flash and DFU are out of scope and are never transmitted;
> everything HelixPy does is recoverable.

---

## Install

**The app** - download the `.dmg` from [Releases][releases], drag HelixPy to
Applications, then **right-click it and choose Open** the first time.
([Why?](docs/install.md#why-macos-warns-you))

[releases]: https://github.com/ORogers/HelixPy/releases

**The command line**

```bash
pipx install 'helixpy[usb]'   # plus: brew install libusb
```

You will also need **HX Edit** installed to reach the pedal, and an
**OpenAI API key** to generate anything - the app asks for one the first time
it opens, and saves it. Prefer to stay offline? **Ollama** works too, free and
locally. [Full setup →](docs/guide.md#1-what-you-need)

## Try it

```bash
hlxgen backup --output ~/helix-backup          # once, before anything else
hlxgen describe "spacious ambient clean, long decay"
hlxgen describe "tight prog metal rhythm" --upload --upload-via usb --slot 4
```

Or open the app, pick a slot, type what you want, press Generate.

**[Read the guide →](docs/guide.md)**

## How it works

A tone request becomes a preset in two model calls, however long the chain:

1. **Blocks.** The prompt carries the signal-chain rules, two worked examples,
   and the whole catalog as one `name - based on` line per model. The model
   picks the chain.
2. **Parameters.** A second call sets every block at once, so gain and levels
   are balanced across the chain rather than block by block.

Both answers are constrained to a JSON schema built from the catalog: only real
model names, only in-range values, only listed option labels. Anything still
wrong is re-asked once with the reason attached, and the assembled preset is
validated against the schema *and* the catalog before a byte reaches disk.

Uploading uses a reverse-engineered USB protocol that writes into a chosen slot
and reads back to confirm. [The full trace →](docs/application_flow.md)

## Supported hardware

| | |
| --- | --- |
| **HX Stomp** | Verified. This is what it was built and tested against. |
| Other Helix / HX devices | Untested. The protocol should be the same, but nobody has confirmed it. |

If you have another unit, [a device report](https://github.com/ORogers/HelixPy/issues/new/choose)
is genuinely useful - including one that just says it worked.

## Documentation

| | |
| --- | --- |
| [Guide](docs/guide.md) | Install, first tone, writing good prompts, troubleshooting. |
| [CLI reference](docs/cli.md) | Every command and flag. |
| [Application flow](docs/application_flow.md) | How it works, end to end. |
| [USB protocol](docs/usb_upload_plan.md) | The research behind the device support. |
| [Legal](docs/legal.md) | Trademarks, and which data files are and are not distributed. |
| [Contributing](CONTRIBUTING.md) | Setup, the checks CI runs, house rules. |

## Licence and attribution

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Helix, HX Stomp, HX Edit and Line 6 are trademarks of Yamaha Guitar Group /
Line 6. This project is independent and unaffiliated; it contains no Line 6
source, firmware or SDK, and does not redistribute Line 6's data files.
[The full statement →](docs/legal.md)

Much of the protocol work is distilled from
[**fretwire**](https://github.com/john-baxter-dev/fretwire), whose living
protocol and preset-format specs did the hard part first.
