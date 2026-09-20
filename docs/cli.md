# `hlxgen` reference

Every command, and the flags worth knowing. `hlxgen <command> --help` is always
the authority; this page is the map.

Global flags come before the command:

| Flag | Effect |
| --- | --- |
| `--dataset PATH` | A different model catalog. Defaults to the one bundled with hlxgen. |
| `--hx-edit PATH` | Your `HX Edit.app`, when it is somewhere the usual search misses. `HLXGEN_HX_EDIT` does the same thing for every run. |
| `--version` | Print the installed version. |

---

## Making presets

### `describe` - a tone in plain English

```bash
hlxgen describe "spacious worship clean"
hlxgen describe "tight prog metal rhythm" --llm-backend openai --reasoning-effort medium
hlxgen describe "70s funk clean" --upload --upload-via usb --slot 4
```

Two model calls - blocks, then every parameter at once - and the result is
validated before anything is written. Without `--output` it lands in
`./generated-presets/<name>.hlx`.

| Flag | Default | |
| --- | --- | --- |
| `--llm-backend {ollama,openai}` | `ollama` | Where the thinking happens. |
| `--ollama-model` | `gpt-oss:20b` | Any model `hlxgen llm-models` lists. |
| `--ollama-endpoint` | `http://localhost:11434/api/generate` | |
| `--num-ctx` | `32768` | Raised automatically if a prompt needs more. |
| `--openai-model` | `gpt-5.6-terra` | Or `gpt-5.6-sol` (flagship), `gpt-5.6-luna` (cheapest). |
| `--reasoning-effort` | `low` | `none` to `max`. Higher is slower and usually better. |
| `--name`, `--author` | | Preset metadata. |
| `--output`, `--dry-run` | | Where it goes, or print it instead. |
| `--upload`, `--upload-via`, `--slot` | | See [uploading](#uploading). |

### `generate` - from a chain file

```bash
hlxgen generate docs/examples/basic_chain.json --output clean.hlx
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

Overrides: `--name`, `--author`, `--tempo`, `--device`, `--device-id`,
`--device-version`, `--app-version`, `--template`, `--output`, `--dry-run`.

The first three blocks are assigned to footswitches 1-3 automatically, with
drive, modulation and delay preferred; set `footswitch` on a block to override.

---

## Looking at presets

### `validate`

```bash
hlxgen validate clean.hlx --report results.json
```

Structural schema check plus a semantic pass over every block against the
catalog. Exit 1 if anything fails.

### `inspect`

```bash
hlxgen inspect clean.hlx
```

Prints the signal chain in order: position, model, category, and what it is
based on.

### `models`

```bash
hlxgen models --category Reverb
```

The catalog, optionally filtered to one category.

### `llm-models`

```bash
hlxgen llm-models --llm-backend ollama
```

What is installed and which thinking levels each model supports.

---

## The device

All of these need HX Edit installed - see [legal.md](legal.md) for why - and
`pip install 'helixpy[usb]'`.

### `devices`

```bash
hlxgen devices
hlxgen devices --identify
```

What is attached. Plain enumeration sends the pedal nothing; `--identify` opens
a session on each to confirm it answers.

### `backup` - do this first

```bash
hlxgen backup --output ~/helix-backup
```

Sweeps every slot (`--count` to do fewer, `--bank` for another bank) and writes
each one out. Read-only.

### `pull`

```bash
hlxgen pull --slot 4 --output slot4.msgpack
```

One slot, as the device holds it.

### `push`

```bash
hlxgen push clean.hlx --slot 4 --archive ~/helix-backup/slot4.msgpack
```

**Overwrites the slot.** `--slot` is required and is never guessed.

| Flag | |
| --- | --- |
| `--archive PATH` | Save what is there before replacing it. Worth the habit. |
| `--separate-cabs` | Keep a cab in its own block. By default an amp carries its own cab, as it does on the pedal. |
| `--via {edits,document}` | `edits` (default) is surgical and the only path that can change a block's model. `document` writes the whole preset at once, which suits restoring a backup. |
| `--dry-run [-o PATH]` | Build and report, send nothing. |
| `--no-verify` | Skip the read-back check. Not recommended. |

### `device-audit`

```bash
hlxgen device-audit --report audit.json
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
