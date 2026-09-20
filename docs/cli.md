# `helixgen` reference

Every command, and the flags worth knowing. `helixgen <command> --help` is always
the authority; this page is the map.

Global flags come before the command:

| Flag | Effect |
| --- | --- |
| `--dataset PATH` | A different model catalog. Defaults to the one bundled with helixgen. |
| `--hx-edit PATH` | Your `HX Edit.app`, when it is somewhere the usual search misses. `HELIXGEN_HX_EDIT` does the same thing for every run. |
| `--version` | Print the installed version. |

### The OpenAI key

`describe` uses OpenAI unless told otherwise, and looks for a key in three
places, most specific first:

1. `OPENAI_API_KEY` in the environment.
2. A `.env` file, in the working directory or any directory above it.
3. The config file the desktop app writes - `~/Library/Application
   Support/HelixGen/config.json` on macOS, `~/.config/helixgen/config.json`
   elsewhere.

So a key set up once in the app also works on the command line, and a shell
variable overrides it for a single run. `--llm-backend ollama` needs no key at
all.

---

## Making presets

### `describe` - a tone in plain English

```bash
helixgen describe "spacious worship clean"
helixgen describe "tight prog metal rhythm" --reasoning-effort medium
helixgen describe "70s funk clean" --llm-backend ollama   # local, free, offline
helixgen describe "warm jazz comp" --upload --upload-via usb --slot 4
```

Three model calls - the blocks, then every parameter at once, then the three
snapshots - and the result is validated before anything is written. Without
`--output` it lands in `./generated-presets/<name>.hlx`.

The snapshots are designed for the tone rather than copied: each one switches
blocks on or off and re-dials the parameters it needs, so a described tone
arrives as three usable sounds. `helixgen inspect` shows them side by side.

| Flag | Default | |
| --- | --- | --- |
| `--llm-backend {openai,ollama}` | `openai` | Where the thinking happens. |
| `--ollama-model` | `gpt-oss:20b` | Any model `helixgen llm-models` lists. |
| `--ollama-endpoint` | `http://localhost:11434/api/generate` | |
| `--num-ctx` | `32768` | Raised automatically if a prompt needs more. |
| `--openai-model` | `gpt-5.6-terra` | Or `gpt-5.6-sol` (flagship), `gpt-5.6-luna` (cheapest). |
| `--reasoning-effort` | `low` | `none` to `max`. Higher is slower and usually better. |
| `--name`, `--author` | | Preset metadata. |
| `--output`, `--dry-run` | | Where it goes, or print it instead. |
| `--upload`, `--upload-via`, `--slot` | | See [uploading](#uploading). |

### `generate` - from a chain file

```bash
helixgen generate docs/examples/basic_chain.json --output clean.hlx
```

Takes a JSON or YAML chain specification. The simple form is a title and an
ordered list of model names:

```json
{
  "title": "Sparkling Clean",
  "blocks": ["US Double Nrm", "LA Studio Comp", "Plate Reverb"]
}
```

The full form mirrors the preset structure, letting you set `meta`, `global`,
`input`, `output`, per-block parameters and footswitch assignments. Anything
omitted falls back to the template and the catalog's defaults. See
[`docs/examples/`](examples/) for both.

**Snapshots.** Either form may carry a `snapshots` list - up to three on an HX
Stomp. Each has a `name` (the device shows 10 characters) and a `blocks` map
keyed by the block's zero-based index, saying what that snapshot changes:
`enabled` to switch a block on or off, `parameters` to re-dial it. A block a
snapshot does not mention keeps its own settings.

```json
"snapshots": [
  {"name": "Clean",  "blocks": {"1": {"enabled": false},
                                "2": {"parameters": {"Drive": 0.2}}}},
  {"name": "Rhythm"},
  {"name": "Solo",   "blocks": {"4": {"enabled": true,
                                      "parameters": {"Mix": 0.3}}}}
]
```

[`docs/examples/snapshots_chain.json`](examples/snapshots_chain.json) is a
worked example. A parameter any snapshot changes becomes a snapshot-controlled
parameter on the pedal, so the knob moves when you switch snapshots rather than
the block simply muting.

**Name an option, do not number it.** Write `"Note": "1/8 Dotted"`, not
`"Note": 7`. The number a preset stores is the device's own, which is the
parameter's `min` plus the option's position in HX Edit's list - and several
lists do not start at zero, so a positional guess lands on the wrong option
without anything reporting a problem. A label is resolved through the catalog
and cannot be wrong.

Overrides: `--name`, `--author`, `--tempo`, `--device`, `--device-id`,
`--device-version`, `--app-version`, `--template`, `--output`, `--dry-run`.

Three blocks are assigned to footswitches 1-3 automatically - drive,
modulation and delay first, then the rest in chain order; set `footswitch` on a
block to override, and an explicit assignment always keeps its switch.

---

## Looking at presets

### `validate`

```bash
helixgen validate clean.hlx --report results.json
```

Structural schema check plus a semantic pass over every block against the
catalog. Exit 1 if anything fails.

### `inspect`

```bash
helixgen inspect clean.hlx
```

Prints the signal chain in order: position, model, category, and what it is
based on.

### `models`

```bash
helixgen models --category Reverb
```

The catalog, optionally filtered to one category.

### `llm-models`

```bash
helixgen llm-models --llm-backend ollama
```

What is installed and which thinking levels each model supports.

---

## The device

All of these need HX Edit installed - see [legal.md](legal.md) for why - and
`pip install 'helixgen[usb]'`.

### `devices`

```bash
helixgen devices
helixgen devices --identify
```

What is attached. Plain enumeration sends the pedal nothing; `--identify` opens
a session on each to confirm it answers.

### `backup` - do this first

```bash
helixgen backup --output ~/helix-backup
```

Sweeps every slot (`--count` to do fewer, `--bank` for another bank) and writes
each one out. Read-only.

### `pull`

```bash
helixgen pull --slot 4 --output slot4.msgpack
```

One slot, as the device holds it.

### `push`

```bash
helixgen push clean.hlx --slot 4 --archive ~/helix-backup/slot4.msgpack
```

**Overwrites the slot.** `--slot` is required and is never guessed.

The chain goes over as surgical edits, then the footswitch layout and the three
snapshots follow as a separate whole-document write - the device's edit
operations do not carry either. Snapshot block states are remapped to the slots
the blocks actually landed in, so a state bound to `dsp0.block2` reaches
whichever slot that block ended up occupying.

| Flag | |
| --- | --- |
| `--archive PATH` | Save what is there before replacing it. Worth the habit. |
| `--separate-cabs` | Keep a cab in its own block. By default an amp carries its own cab, as it does on the pedal. |
| `--via {edits,document}` | `edits` (default) is surgical and the only path that can change a block's model. `document` writes the whole preset at once, which suits restoring a backup. |
| `--dry-run [-o PATH]` | Build and report, send nothing. |
| `--no-verify` | Skip the read-back check. Not recommended. |

### `device-audit`

```bash
helixgen device-audit --report audit.json
```

Reconciles the bundled catalog against `Helix.sym` from your own HX Edit
install: which models resolve, which split into Mono and Stereo variants, and
whose parameter lists do not line up. Diagnostic - it always exits 0, because a
real symbol table carries host-only models that legitimately have no device
symbol.

---

## Uploading from `generate` and `describe`

Both accept the same flags:

| Flag | |
| --- | --- |
| `--upload` | Send it after writing. |
| `--upload-via {usb,applescript}` | Defaults to USB when a device is attached. |
| `--slot N` | Required for USB. |
| `--upload-script`, `--upload-mode` | For the AppleScript fallback, which drives HX Edit's own interface and cannot target a slot - its `auto` mode always overwrites slot 1. |

---

## Exit codes

| | |
| --- | --- |
| 0 | Success. |
| 1 | Validation failed, an LLM call failed, or an upload failed. |
| 2 | Bad arguments, an unreadable dataset or schema, or a chain the generator rejects. |
