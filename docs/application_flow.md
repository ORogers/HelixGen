# How HelixGen currently works

A trace of every path an invocation can take, from `helixgen` on the command line or
a click in the desktop app, to a written `.hlx` file and a slot on the pedal.

Two front ends share one engine. `helixgen` is a single-process CLI: every invocation
loads its data files, does one job, prints to stdout/stderr, and exits. The desktop
app (`helixgen_ui`) is long-lived, runs its blocking work on Qt threads, and caches
what a slot read would otherwise pay for twice — but it calls the same functions,
never the CLI, and never a subprocess.

There is one persistent piece of state in either: `helixgen/config.py`, a small JSON
file holding what a person set once and should not have to set again - where HX Edit
was found, the desktop app's settings, and the OpenAI key. Everything else is an
argument or a file path.

## Data inputs

The first three ship inside the package (`helixgen/data/`) and are resolved through
`helixgen.resources`, so they are found wherever helixgen is installed rather than
relative to the working directory. The last two are Line 6's, are **not**
distributed, and are read from the user's own HX Edit install — see
[legal.md](legal.md).

| File | Loaded by | What it decides |
| --- | --- | --- |
| `helix_model_information.json` | `ModelCatalog` | Which models exist, their internal names, categories, and every parameter's type, range, default and option map. Nothing else may name a model. **An option map is keyed by the number the device stores**, not by the option's position in HX Edit's display list - several lists do not start at zero. |
| `HXTemplate.hlx` | `load_json_file` | The structural skeleton every generated preset is deep-copied from: `meta`, `tone.global`, `dsp0`, `dsp1`, `snapshot0–2`, `variax`, and the HX Stomp device IDs. |
| `helix-preset.schema.json` | `PresetValidator` | The structural contract checked before any file is written — required `meta` keys, `tone.global.@tempo`, and `dsp0.inputA` / `outputA`. |
| `Helix.sym` (in HX Edit) | `DeviceSymbols` | The array position of each model *is* its identity on the wire, and each variant's parameter order. Required by every device operation; located by `helixgen/device/hxedit.py`. |
| `amp.models` (in HX Edit) | `AmpDefaults` | Which cab each amp carries in its own slot. Optional — without it an amp keeps a separate cab block. |

## 1. Entry and dispatch

`helixgen/__main__.py` imports `helixgen.main`, which forwards to `cli.main()`; an
installed copy reaches the same place through the `helixgen` console script.
`build_parser()` builds one `argparse` parser with a required subcommand, then
`main()` dispatches on `args.command` (`cli.py:main`) across **eleven** commands.
`generate` and `describe` differ only in how they obtain a chain dictionary; from
there they share one code path.

The four device commands — `devices`, `pull`, `backup`, `push` — are dispatched
through a function-local import of `helixgen.device.commands`, which is what keeps
pyusb an optional dependency: the plain CLI never imports it. `--hx-edit`, if
given, is validated and applied to the environment before any command runs, so no
handler has to carry a path for the one run where HX Edit is somewhere unusual.

```mermaid
flowchart TD
    entry["python -m helixgen &lt;args&gt;"] --> parse["cli.main()<br>build_parser() → parse_args(argv)"]
    parse --> disp{{"dispatch on args.command"}}

    disp --> models["models"]
    disp --> inspect["inspect"]
    disp --> validate["validate"]
    disp --> generate["generate"]
    disp --> describe["describe"]
    disp --> devcmds["devices · pull · backup · push<br>lazy import: pyusb stays optional"]
    disp --> audit["device-audit · llm-models"]

    models --> m1["ModelCatalog(dataset)<br>sort, filter, box table"] --> mx(["exit 0"])
    inspect --> i1["load_json_file()<br>inspect_preset()"] --> ix(["exit 0"])
    validate --> v1["load_json_file()<br>validate_structural + validate_semantic"] --> v2["--report → JSON"] --> vx(["exit 0 / 1"])

    generate --> g1["load_chain_spec()<br>.json · .yaml · .hlxchain"] --> spine
    describe --> d1["2 LLM round-trips<br>(see section 2)"] --> spine

    spine["_generate_from_chain(chain, args, catalog, template)"]
    spine --> gp["generator.generate_preset()<br>template ⊕ dataset defaults ⊕ chain parameters"]
    gp -. clamp warnings .-> warn["stderr: '… clamped to maximum'"]
    gp --> gate{{"PresetValidator:<br>structural + semantic"}}
    gate -- errors --> err["print errors to stderr<br>exit 1 · nothing written"]
    gate -- clean --> dry{{"--dry-run ?"}}
    dry -- yes --> dryout["print preset JSON to stdout<br>exit 0"]
    dry -- no --> write["resolve output path · mkdir -p · json.dump(indent=2)"]
    write --> up{{"--upload ?"}}
    up -- yes --> tr{{"--upload-via"}}
    tr -- "usb (default when attached)" --> usbw["apply_tone → surgical edits<br>see section 5"] --> ok(["exit 0"])
    tr -- "applescript" --> osa["subprocess → osascript<br>drives HX Edit's UI (macOS only)"] --> ok
    up -- no --> ok

    devcmds --> dev1["helixgen.device.usb Session<br>read, back up or write slots"] --> devx(["exit 0 / 1"])
    audit --> a1["DeviceSymbols ⋈ ModelCatalog<br>reconciliation report"] --> ax(["exit 0"])
```

Output paths: `generate` writes `<chain>.hlx` beside the chain file, `describe`
writes to `resources.default_output_dir()` — `./generated-presets/` from a shell,
`~/Documents/HelixGen/presets/` inside a frozen app bundle, which has no meaningful
working directory. Either is overridden by `--output`.

## 2. Getting a chain dictionary

A "chain" is a plain dict with an ordered `blocks` list plus optional `meta`,
`global`, `input` and `output` sections. `generate` reads one off disk — `.json` and
`.hlxchain` go through `json.load`, `.yaml`/`.yml` go through PyYAML, and any other
suffix is rejected before the file is opened.

`describe` synthesises one instead, and this is where the tool spends nearly all of
its wall-clock time. The desktop UI (`helixgen_ui.generation.generate_tone`) takes the
same path. It makes **three model calls** however long the chain is: one to pick the
blocks, one to set every block's parameters, and one to design the preset's three
snapshots. All three go through the same `call_llm` closure, so the backend, model
and thinking level apply to every round.

```mermaid
flowchart TD
    start["helixgen describe '&lt;tone request&gt;' --llm-backend …"] --> pick{{"_build_llm_caller(backend)"}}
    pick -- "'openai' (default)" --> oa["client.responses.create(model, input,<br>reasoning.effort, strict json_schema)<br>key: environment → .env → config file"]
    pick -- "'ollama'" --> ol["POST /api/generate · stream=false<br>format = response schema (not for gpt-oss)<br>think = level · options.num_ctx · keep_alive 30m"]

    ol --> closure["call_llm(LLMRequest: prompt + schema) → str"]
    oa --> closure

    closure --> r1["ROUND 1 - block selection<br>_compose_prompt(): rules, two few-shot pairs from real catalog<br>names, the catalog as 'name - based on' lines by category,<br>then the user goal last (cacheable prefix)<br>schema: blocks ∈ catalog display names"]
    r1 --> parse["_parse_chain_response()<br>json.loads (fence/brace fallback) → catalog.get(name) per block"]
    parse -- "rejected" --> retry1["re-ask once with the reason appended"] --> parse
    parse -- "rejected twice" --> fail(["LLMGenerationError → exit 1"])
    parse --> r2["ROUND 2 - all parameters in one call<br>cabs and blocks without non-@ parameters keep defaults<br>schema: block1…blockN → every parameter nullable,<br>continuous values in range, options by label,<br>unlabelled discrete parameters as integers in the device's range"]
    r2 --> norm["_parse_parameters_response() → _normalize_parameters()<br>null = keep default · labels mapped to option numbers"]
    norm -- "rejected" --> retry2["re-ask once with the reason appended"] --> norm
    norm --> r3["ROUND 3 - snapshot design<br>every block by position with its chosen values, and the<br>parameters a snapshot controller can sweep<br>schema: exactly 3 named snapshots (≤10 chars), each block's<br>enabled and parameters nullable"]
    r3 --> snap["_parse_snapshots_response()<br>null = leave as the block has it · one-based block keys<br>become zero-based chain indexes"]
    snap -- "rejected" --> retry3["re-ask once with the reason appended"] --> snap
    snap --> out["chain dict → the shared spine in section 1"]
```

The catalog is the only authority on names: the schema only admits catalog names,
and a name that still slips through (gpt-oss on Ollama runs without a schema,
because Ollama 0.33 returns empty text for it whenever one is set) is re-asked once
and then aborts the run before a preset is built. Every call is timed; the Ollama
and OpenAI token counts are logged, and the UI's Generation panel shows each
round's duration.

## 3. Assembling the preset

`generate_preset()` is where the three data sources meet. It deep-copies the
template, then writes a `block0…blockN` payload into `tone.dsp0` for each entry in
the chain. The order the four value sources are applied in is the whole mechanism —
in particular, the template's own keys for a given block are applied **last** and
only via `setdefault`, so they fill gaps rather than override anything you or the
model chose.

```mermaid
flowchart TD
    tpl["HXTemplate.hlx<br>meta · tone.global · dsp0 · dsp1<br>snapshot0–2 · variax · device ids"] --> gp
    cat["helix_model_information.json<br>ModelDefinition per model<br>ParameterDefinition per control"] --> gp
    chain["chain dict<br>blocks[] · meta · global · input · output<br>plus CLI overrides"] --> gp

    gp["generate_preset(chain, catalog, template, overrides)<br>→ (preset, GenerationReport)"]
    gp --> l1["1 · identity keys from the block spec or its defaults<br>@model @path @position @type @enabled @stereo @no_snapshot_bypass"]
    l1 --> l2["2 · dataset default for every catalog parameter the spec did not supply"]
    l2 --> l3["3 · supplied parameters, normalized and clamped<br>out-of-range values raise a warning"]
    l3 --> l4["4 · the template's own keys for that blockN, via setdefault<br>gap-fill only, never an override"]
    l4 --> payload["tone.dsp0.block0 … blockN"]

    payload --> fs["FOOTSWITCH ASSIGNMENT<br>an explicit block 'footswitch' index always wins<br>every remaining block queues for footswitches 1–3<br>distortion, modulation and delay go to the front of that queue<br>anything past switch 3 gets none"]
    payload --> sn["SNAPSHOTS<br>_apply_snapshots(): each tone.snapshotN records every blockN's<br>bypass state, defaulting to the block's own @enabled<br>a parameter any snapshot changes gets a tone.controller entry<br>(@controller 9 = Snapshots, @min/@max = its range) and every<br>snapshot stores its own @value<br>dsp0 is then synced to the @current_snapshot"]
    payload --> md["METADATA<br>device · device_version · appversion via _coerce_numeric<br>ints, floats and 0x… hex accepted; other strings fall back to template<br>dsp1 and variax ride through from the template untouched"]
```

Only `dsp0` is ever rebuilt: a chain cannot currently place blocks on the second
signal path.

### Snapshots

A chain may carry a `snapshots` list, one entry per template snapshot (three on HX
Stomp). Each entry has an optional `name` (the device shows at most 10 characters;
longer ones are truncated with a warning) and a `blocks` map keyed by the block's
zero-based index in the chain:

```json
"snapshots": [
  {"name": "Clean", "blocks": {"1": {"enabled": false}, "2": {"parameters": {"Drive": 0.2}}}},
  {"name": "Rhythm"},
  {"name": "Solo", "blocks": {"4": {"enabled": true, "parameters": {"Mix": 0.35}}}}
]
```

The block's own `enabled` and `parameters` are the baseline; a snapshot lists only
what it changes. This mirrors how HX Edit stores them: every block's bypass state
lives in `snapshotN.blocks.dsp0`, and a parameter becomes snapshot-controlled
through a `tone.controller.dsp0.blockN.<param>` entry with `@controller: 9`, after
which every snapshot holds its own `@value` in `snapshotN.controllers`. A parameter
whose range the catalog does not know cannot carry a controller and is rejected, as
is a bypass change on a block marked `no_snapshot_bypass`. A chain without
`snapshots` gets the template's, with each block's `@enabled` copied in.

## 4. The validation gate

Nothing reaches disk unvalidated. `PresetValidator` runs two independent passes over
the assembled preset and concatenates their errors; a single error is enough to print
to stderr and exit 1 with no file written. The same validator backs the standalone
`validate` subcommand, so a generated preset and a hand-written one are held to
identical standards.

| Pass | Reads | Rejects |
| --- | --- | --- |
| `validate_structural` | `helix-preset.schema.json`, through `SimpleSchemaValidator` — a hand-rolled subset supporting `type`, `required`, `properties`, `additionalProperties`, `items` and `minItems` | A missing `meta.application`, `meta.appversion`, `meta.name`, `tone.global.@tempo`, `@current_snapshot`, `dsp0.inputA` or `dsp0.outputA`; wrong JSON types. Booleans are deliberately not counted as numbers. |
| `validate_semantic` | The model catalog, walking every entry in `tone.dsp0`, `tone.controller` and each `tone.snapshotN` | A block whose `@model` is absent from the catalog, any non-`@` key that is not a real parameter of that model, or a value the parameter definition refuses to normalize. Snapshot bypass states and controller values must name blocks that exist, and every snapshot value needs a matching `tone.controller` assignment. Names starting `HelixStomp_AppDSPFlow` or `HD2_AppDSPFlow` are treated as routing infrastructure and skipped, which is how `inputB`/`outputB` pass without catalog entries. |

## 5. Writing out, and getting it onto the pedal

With the gate cleared, `--dry-run` prints the preset as indented JSON and stops.
Otherwise the output path resolves, parent directories are created, and the preset is
written with `json.dump(indent=2)`. Whatever happens next, the file is already safely
on disk — every upload failure is reported against a preset that still exists.

Two transports can follow. `--upload-via` picks one; left off, it takes USB when a
device is attached and falls back to the AppleScript uploader otherwise.

### 5a. USB — the real path

`helixgen push`, `generate/describe --upload-via usb`, and the desktop app's Upload
button all converge on `helixgen.device.editor.apply_tone`. It is the only transport
that can change a block's **model**, which is exactly what a freshly generated preset
does to whatever was in the slot.

Underneath it, `helixgen/device/usb.py` opens **interface 0 only** — the audio and MIDI
interfaces are left alone — and runs one strictly synchronous request/response
channel. Three properties of the device shape that layer:

* **Nothing is pipelined.** One request, one response.
* **Replies are matched by the transaction id echoed at MessagePack key 102**, never
  by arrival order. The device interleaves keepalives, flow-control credits and
  leftover stream chunks on the same channel, and taking one of those for an
  acknowledgement mis-attributes every later reply.
* **A dropped handle can leave the pedal's front panel locked**, which is why the
  session is closed politely on the way out and the context manager is the intended
  way in. Every bulk operation carries a timeout, because an unbounded write to a
  device that has stopped draining its OUT endpoint blocks forever.

```mermaid
flowchart TD
    start["apply_tone(preset, symbols, slot=N)"] --> sym["Helix.sym → DeviceSymbols<br>model name → wire ordinal<br>Mono/Stereo variant decides parameter order"]
    sym --> sel["EditSession: select slot, read its edit buffer<br>unpopulated slot → refuse, nothing written"]
    sel --> plan["plan_chain(): catalog names → device symbols<br>amp.models fuses each amp's cab into its own slot<br>(--separate-cabs keeps them apart)"]
    plan --> clear["Slots the donor fills that the tone does not:<br>delete, so the result is the generated chain<br>and not a merge of two"]
    clear --> swap["Per block, in order:<br>swap the model — which resets that block to the<br>new model's defaults — then send the tone's values"]
    swap --> commit["Commit, then read back and compare<br>(--no-verify skips the comparison)"]
    commit --> layout["Second session: whole-document write for the<br>footswitch layout and the snapshots, which the<br>edit ops do not carry. Block states are remapped<br>through the slots the blocks actually landed in."]
    layout --> done(["EditReport → printed, or shown in the UI"])
```

Positions are assigned in chain order rather than taken from the tone's own
`@position`: fusing a cab into its amp closes a gap, and leaving one would waste the
slot the fusion just freed.

The layout write is a second session on purpose: the edit run above has spent most of
its frame budget, and this is another dozen frames on top of a full read. It is also
the only write that is safe to send whole, because by that point no model is changing.
Snapshot names, tempos and valid flags go back as the preset has them, while each
snapshot's per-block on/off states are looked up through `placed` — a switch or a
snapshot state bound to `dsp0.block2` has to reach whichever device slot that block
actually occupies, and the two are independent on the wire.

`--archive` writes the slot's current contents out before overwriting it. `helixgen
backup` and `helixgen pull` are the read-only halves of the same transport.

**Firmware, flash and DFU are out of scope and are never transmitted.** See
[`usb_protocol.md`](usb_protocol.md) §7 for the rest of the safety rules.

### 5b. AppleScript — the fallback

`--upload-via applescript` predates the USB work and survives as a fallback. It is
macOS-only, cannot read anything back, and cannot target a slot: there is no API
here, only HX Edit's own user interface driven through System Events on fixed
delays.

1. `activate` — bring HX Edit to the front, then wait 3 seconds for it to settle.
2. In `manual` mode, show a dialog and pause so the user can highlight the target
   slot; in `auto` mode, press the first row of the preset outline, then `⌘↑` and
   Return to select slot 1 — overwriting it.
3. `⌘I` — open HX Edit's *Import Preset…* sheet.
4. `⌘⇧G` — open the macOS *Go to Folder* field.
5. Type the preset's absolute path, then Return to accept it.
6. Three further Returns: dismiss the sheet, choose the file, confirm the import.

If any step fails, `_generate_from_chain` catches it, prints `Upload failed: …` and
returns 1.

## 6. The desktop app

`helixgen_ui` is a PySide6 window over the same engine. It deliberately does **not**
shell out to `helixgen`, and does not call `cli._generate_from_chain` either — that
function prints its result and returns an exit code, which a UI would have to scrape.
`helixgen_ui/generation.py` calls the same underlying pieces the `describe` command
calls and hands back a typed `GenerationResult`.

```mermaid
flowchart TD
    win["MainWindow"] --> panels["SlotPanel · PromptPanel · GenerationPanel<br>PreviewPanel · UploadPanel · SettingsPage · SetupPage"]
    panels --> workers["helixgen_ui/workers.py — one QThread per blocking job"]

    workers --> gw["GenerationWorker → generation.generate_tone()<br>the section 2–4 spine, unchanged"]
    workers --> kw["ApiKeyTestWorker → verify_openai_api_key()<br>lists models: the cheapest authenticated call"]
    workers --> dw["DeviceScanWorker → find_device()<br>enumeration only; sends the pedal nothing"]
    workers --> sw["SlotReadWorker · SlotChainWorker → read_slots / read_slot_chain"]
    workers --> uw["UsbUploadWorker → push_preset → apply_tone (section 5a)"]
    workers --> aw["ScriptUploadWorker → the AppleScript fallback"]

    gw --> ui["Qt signals back to the UI thread"]
    kw --> ui
    dw --> ui
    sw --> ui
    uw --> ui
    aw --> ui
```

Three things about it are not obvious from the signatures:

- **Every live worker is held in a module-level set** (`workers._LIVE_WORKERS`).
  Destroying a `QThread` while its thread is still running aborts the process, and at
  interpreter teardown Python will happily collect a worker whose only reference was
  a closed window's attribute.
- **Three of the four costs in a slot read never change between clicks**, so
  `helixgen_ui/device.py` caches them: `_SYMBOLS_CACHE` (the symbol table),
  `_CATALOG_CACHE` (the model catalog) and `_LISTING_CACHE` (the bank's name
  listing). Only the document itself comes off the wire each time. `invalidate_caches`
  drops the listing after an upload, because an upload is the one thing that renames
  a slot.
- **Upload is gated on finding HX Edit**, not on attempting it. `SettingsPage`
  re-runs discovery whenever it opens and emits `hx_edit_changed`; without an install
  the Upload button is disabled and says why, and auto-upload declines rather than
  failing inside the worker.

Settings are written to `helixgen/config.py`'s file on every change, so a choice made
once survives a restart. Each field is validated on its own type when read back, so
an outdated config costs the one setting it got wrong rather than resetting
everything.

**First run.** OpenAI is the default backend and it needs a key. With no key and no
backend chosen, `MainWindow` opens on `SetupPage` instead of the workspace: what the
two backends cost, how to make a key, a field to paste it into, and a "Use Ollama
instead" button that records that choice so the page does not return. Pressing
Generate without a key routes there too, rather than spending a round trip to fail
on a key that was never set.

## The read-only commands

Five subcommands never write anything:

- `models` loads the catalog, optionally filters on an exact case-insensitive
  category match, and prints a box-drawn table of display name, internal ID and
  real-world reference.
- `inspect` loads a preset, resolves each `tone.dsp0` block back through the catalog,
  and prints the chain sorted by `@position`. Unresolvable models are shown as-is
  under the type `Unknown` rather than failing. Note that `inspect` declares its own
  `--dataset` flag, so both `helixgen inspect p.hlx --dataset X` and
  `helixgen --dataset X inspect p.hlx` are valid, and the subcommand's value wins.
- `validate` runs the gate from section 4 on an existing file and can write its
  findings as JSON with `--report`.
- `llm-models` lists what the chosen backend offers — a fixed tuple for OpenAI, and
  whatever the Ollama server reports as installed — with the thinking levels each
  one supports.
- `device-audit` reconciles the catalog against `Helix.sym` from the user's HX Edit
  install: which models resolve, which the device splits into Mono and Stereo
  variants, and whose parameter lists do not line up. It always exits 0, because a
  real symbol table carries host-only models that legitimately have no device symbol,
  so the findings are reported rather than turned into a failure.

`devices` is nearly read-only: plain enumeration sends the pedal nothing at all, and
only `--identify` opens a session. `pull` and `backup` read slots without writing.

## Behaviour that isn't visible in the signatures

- **Return codes are discarded under `python -m helixgen`.** `__main__.py` calls
  `helixgen.main()`, which calls `cli.main()` and drops its integer result. Only
  `python helixgen/cli.py` wraps it in `SystemExit`. Argparse's own failures still
  exit 2, because `parser.error()` exits directly.
- **`ModelCatalogError` and `ValidationError` surface as argparse errors.** `main()`
  catches both and routes them to `parser.error()`, so a missing dataset, a malformed
  chain or an unknown model in a hand-written chain prints usage and exits 2 — while
  an LLM failure, caught separately in `run_describe`, exits 1.
- **A non-numeric `--device` silently falls back.** The string lands in `meta.device`
  as written, but it is also a candidate for the numeric `data.device` field;
  `_coerce_numeric` accepts ints, floats and `0x…` hex, and returns the template's
  value for anything else rather than raising.
- **An option's label and its stored number are not the same thing.** The catalog's
  option maps are keyed by the value the device stores, which is the parameter's own
  `min` plus the option's position in HX Edit's display list - and several lists do
  not start at zero. A delay's note sync runs 1..19, so the dotted eighth at display
  position 7 is stored as 8; 7 is the quarter triplet. Nothing complains about the
  wrong one, because it is still a valid option, which is why this was only found by
  checking 32 HX Edit-written presets.
- **A discrete parameter with no labels is validated against its range** rather than
  passed through unchecked, and the LLM schema constrains it to an integer within
  that range instead of any number.
- **Enum defaults are guessed when the dataset omits one.**
  `ParameterDefinition.default_value()` prefers an option labelled `False`, then one
  labelled `Off`, then the first entry in the forward map.
- **The OpenAI client is a module-level singleton.** `_OPENAI_CLIENT` is built on
  first use and reused for the rest of the process, so the key is resolved once per
  run. `store_openai_api_key` drops it, because a key changed in a running app
  would otherwise be ignored until the next launch.
- **The API key has three sources, most specific first**: `OPENAI_API_KEY` in the
  environment, a `.env` file found by walking up from the working directory, then
  `helixgen/config.py`'s file. The environment winning is what lets a shell override a
  saved key for one run; the config file is what lets an app launched from the Dock,
  with no environment at all, have a key in the first place. It is stored in plain
  text in a file created `rw-------`.
- **Cab blocks are never parameterised by the model.** Any model whose category
  contains "cab" keeps its dataset defaults; their controls (and the IR slot lists)
  rarely carry the tone request.
- **HX Edit is found once and remembered.** `helixgen/device/hxedit.py` tries an
  explicit path, `HELIXGEN_HX_EDIT`, a path remembered in `helixgen/config.py`, the two
  standard `/Applications` locations, then Spotlight by bundle id. A remembered path
  is re-validated on every use, so an install that moved falls through to a fresh
  search rather than pinning the app to a dead path, and an install is recognised by
  the presence of `Helix.sym` rather than by its name.
- **A missing `amp.models` is not fatal.** Without it an amp simply keeps its cab in
  a separate block, which is what the tone already describes. A missing `Helix.sym`
  is fatal to anything touching the device, because nothing can be addressed without
  it.

## Exit codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| 0 | every `run_*` on success | Table printed, preset valid, or preset written (and uploaded). |
| 1 | `run_validate`, `_generate_from_chain`, `run_describe`, the device commands | Validation errors, an LLM failure, no device, no HX Edit install, or a failed upload. |
| 2 | `parser.error()` | Bad arguments, a missing dataset or schema, or a chain the generator rejects outright. |

## CI

`.github/workflows/ci.yml` runs four jobs against Python 3.13:

- **tests** — the pytest suite, headless via `QT_QPA_PLATFORM=offscreen`.
- **cli round-trip** — installs the built package, runs from a directory that is not
  the checkout (which is what catches a path silently resolving against the working
  directory), then generates and validates every chain in `docs/examples/`.
- **lint** — `ruff check` pinned to a fixed version so a ruff release can't fail the
  build on its own.
- **build** — builds the wheel and sdist and fails if a data file has fallen out of
  the wheel, since that installs cleanly and then breaks on first use.

Nothing in CI touches an LLM backend, a USB device or an HX Edit install — the
`describe` path is covered by the unit tests' stubs, and the device tests drive the
codec, framing and document parsing against fixtures.
