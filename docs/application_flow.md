# How `hlxgen` currently works

A trace of every path an invocation can take, from `python -m hlxgen` to a written
`.hlx` file — and, on macOS, to a preset slot inside HX Edit.

`hlxgen` is a single-process CLI with no persistent state. Every invocation loads its
data files from disk, does one job, prints to stdout/stderr, and exits. There is no
server, no cache and no config file: everything is an argument or a file path.

## Data inputs

| File | Loaded by | What it decides |
| --- | --- | --- |
| `helix_model_information.json` | `ModelCatalog` | Which models exist, their internal names, categories, and every parameter's type, range, default and enum map. Nothing else may name a model. |
| `HXTemplate.hlx` | `load_json_file` | The structural skeleton every generated preset is deep-copied from: `meta`, `tone.global`, `dsp0`, `dsp1`, `snapshot0–2`, `variax`, and the HX Stomp device IDs. |
| `helix-preset.schema.json` | `PresetValidator` | The structural contract checked before any file is written — required `meta` keys, `tone.global.@tempo`, and `dsp0.inputA` / `outputA`. |

## 1. Entry and dispatch

`hlxgen/__main__.py` imports `hlxgen.main`, which forwards to `cli.main()`.
`build_parser()` builds one `argparse` parser with a required subcommand, then
`main()` dispatches on `args.command` to one of five `run_*` functions. `generate`
and `describe` differ only in how they obtain a chain dictionary; from there they
share one code path.

```mermaid
flowchart TD
    entry["python -m hlxgen &lt;args&gt;"] --> parse["cli.main()<br>build_parser() → parse_args(argv)"]
    parse --> disp{{"dispatch on args.command"}}

    disp --> models["models"]
    disp --> inspect["inspect"]
    disp --> validate["validate"]
    disp --> generate["generate"]
    disp --> describe["describe"]

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
    write --> up{{"--upload ? (darwin only)"}}
    up -- yes --> osa["subprocess → osascript<br>drives HX Edit's UI"] --> ok(["exit 0"])
    up -- no --> ok
```

Output paths: `generate` writes `<chain>.hlx` beside the chain file, `describe`
writes `./generated-presets/<slugified-preset-name>.hlx`. Either is overridden by
`--output`.

## 2. Getting a chain dictionary

A "chain" is a plain dict with an ordered `blocks` list plus optional `meta`,
`global`, `input` and `output` sections. `generate` reads one off disk — `.json` and
`.hlxchain` go through `json.load`, `.yaml`/`.yml` go through PyYAML, and any other
suffix is rejected before the file is opened.

`describe` synthesises one instead, and this is where the tool spends nearly all of
its wall-clock time. The desktop UI (`hlxgen_ui.generation.generate_tone`) takes the
same path. It makes **two model calls** however long the chain is: one to pick the
blocks, then one to set every block's parameters. Both go through the same
`call_llm` closure, so the backend, model and thinking level apply to both rounds.

```mermaid
flowchart TD
    start["hlxgen describe '&lt;tone request&gt;' --llm-backend …"] --> pick{{"_build_llm_caller(backend)"}}
    pick -- "'ollama' (default)" --> ol["POST /api/generate · stream=false<br>format = response schema (not for gpt-oss)<br>think = level · options.num_ctx · keep_alive 30m"]
    pick -- "'openai'" --> oa["client.responses.create(model, input,<br>reasoning.effort, strict json_schema)<br>OPENAI_API_KEY from env, else a .env scan"]

    ol --> closure["call_llm(LLMRequest: prompt + schema) → str"]
    oa --> closure

    closure --> r1["ROUND 1 - block selection<br>_compose_prompt(): rules, two few-shot pairs from real catalog<br>names, the catalog as 'name - based on' lines by category,<br>then the user goal last (cacheable prefix)<br>schema: blocks ∈ catalog display names"]
    r1 --> parse["_parse_chain_response()<br>json.loads (fence/brace fallback) → catalog.get(name) per block"]
    parse -- "rejected" --> retry1["re-ask once with the reason appended"] --> parse
    parse -- "rejected twice" --> fail(["LLMGenerationError → exit 1"])
    parse --> r2["ROUND 2 - all parameters in one call<br>cabs and blocks without non-@ parameters keep defaults<br>schema: block1…blockN → every parameter nullable,<br>numbers in range, options by label"]
    r2 --> norm["_parse_parameters_response() → _normalize_parameters()<br>null = keep default · labels mapped to option numbers"]
    norm -- "rejected" --> retry2["re-ask once with the reason appended"] --> norm
    norm --> out["chain dict → the shared spine in section 1"]
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
    payload --> sn["SNAPSHOTS AND METADATA<br>every tone.snapshot* gets each blockN set true by setdefault<br>device · device_version · appversion via _coerce_numeric<br>ints, floats and 0x… hex accepted; other strings fall back to template<br>dsp1 and variax ride through from the template untouched"]
```

Only `dsp0` is ever rebuilt: a chain cannot currently place blocks on the second
signal path.

## 4. The validation gate

Nothing reaches disk unvalidated. `PresetValidator` runs two independent passes over
the assembled preset and concatenates their errors; a single error is enough to print
to stderr and exit 1 with no file written. The same validator backs the standalone
`validate` subcommand, so a generated preset and a hand-written one are held to
identical standards.

| Pass | Reads | Rejects |
| --- | --- | --- |
| `validate_structural` | `helix-preset.schema.json`, through `SimpleSchemaValidator` — a hand-rolled subset supporting `type`, `required`, `properties`, `additionalProperties`, `items` and `minItems` | A missing `meta.application`, `meta.appversion`, `meta.name`, `tone.global.@tempo`, `@current_snapshot`, `dsp0.inputA` or `dsp0.outputA`; wrong JSON types. Booleans are deliberately not counted as numbers. |
| `validate_semantic` | The model catalog, walking every entry in `tone.dsp0` | A block whose `@model` is absent from the catalog, any non-`@` key that is not a real parameter of that model, or a value the parameter definition refuses to normalize. Names starting `HelixStomp_AppDSPFlow` or `HD2_AppDSPFlow` are treated as routing infrastructure and skipped, which is how `inputB`/`outputB` pass without catalog entries. |

## 5. Writing out, and the HX Edit handoff

With the gate cleared, `--dry-run` prints the preset as indented JSON and stops.
Otherwise the output path resolves, parent directories are created, and the preset is
written with `json.dump(indent=2)`.

Only `describe --upload` goes further. The CLI checks `sys.platform == "darwin"`,
locates `osascript` on `PATH`, and runs `scripts/import_helix_preset.applescript`
with the preset path and a mode. There is no API here — the script drives HX Edit's
actual user interface through System Events, on fixed delays:

1. `activate` — bring HX Edit to the front, then wait 3 seconds for it to settle.
2. In `manual` mode, show a dialog and pause so the user can highlight the target
   slot; in `auto` mode, press the first row of the preset outline, then `⌘↑` and
   Return to select slot 1 — overwriting it.
3. `⌘I` — open HX Edit's *Import Preset…* sheet.
4. `⌘⇧G` — open the macOS *Go to Folder* field.
5. Type the preset's absolute path, then Return to accept it.
6. Three further Returns: dismiss the sheet, choose the file, confirm the import.

If any step fails, `_generate_from_chain` catches it, prints `Upload failed: …` and
returns 1 — the preset file itself is already safely on disk by then.

## The read-only commands

Three subcommands never write a preset:

- `models` loads the catalog, optionally filters on an exact case-insensitive
  category match, and prints a box-drawn table of display name, internal ID and
  real-world reference.
- `inspect` loads a preset, resolves each `tone.dsp0` block back through the catalog,
  and prints the chain sorted by `@position`. Unresolvable models are shown as-is
  under the type `Unknown` rather than failing. Note that `inspect` declares its own
  `--dataset` flag, so both `hlxgen inspect p.hlx --dataset X` and
  `hlxgen --dataset X inspect p.hlx` are valid, and the subcommand's value wins.
- `validate` runs the gate from section 4 on an existing file and can write its
  findings as JSON with `--report`.

## Behaviour that isn't visible in the signatures

- **Return codes are discarded under `python -m hlxgen`.** `__main__.py` calls
  `hlxgen.main()`, which calls `cli.main()` and drops its integer result. Only
  `python hlxgen/cli.py` wraps it in `SystemExit`. Argparse's own failures still
  exit 2, because `parser.error()` exits directly.
- **`ModelCatalogError` and `ValidationError` surface as argparse errors.** `main()`
  catches both and routes them to `parser.error()`, so a missing dataset, a malformed
  chain or an unknown model in a hand-written chain prints usage and exits 2 — while
  an LLM failure, caught separately in `run_describe`, exits 1.
- **A non-numeric `--device` silently falls back.** The string lands in `meta.device`
  as written, but it is also a candidate for the numeric `data.device` field;
  `_coerce_numeric` accepts ints, floats and `0x…` hex, and returns the template's
  value for anything else rather than raising.
- **Enum defaults are guessed when the dataset omits one.**
  `ParameterDefinition.default_value()` prefers an option labelled `False`, then one
  labelled `Off`, then the first entry in the forward map.
- **The OpenAI client is a module-level singleton.** `_OPENAI_CLIENT` is built on
  first use and reused for the rest of the process, so the key is resolved once per
  run.
- **Cab blocks are never parameterised by the model.** Any model whose category
  contains "cab" keeps its dataset defaults; their controls (and the IR slot lists)
  rarely carry the tone request.

## Exit codes

| Code | Raised by | Meaning |
| --- | --- | --- |
| 0 | every `run_*` on success | Table printed, preset valid, or preset written (and uploaded). |
| 1 | `run_validate`, `_generate_from_chain`, `run_describe` | Validation errors, an LLM failure, a non-macOS `--upload`, or a failed upload. |
| 2 | `parser.error()` | Bad arguments, a missing dataset or schema, or a chain the generator rejects outright. |

## CI

`.github/workflows/ci.yml` runs three jobs against Python 3.13: the pytest suite; a
round-trip that generates and validates every chain in `docs/examples/` and then
exercises `inspect` and `models`; and `ruff check` pinned to a fixed version so a
ruff release can't fail the build on its own. Nothing in CI touches an LLM backend —
the `describe` path is covered only by the unit tests' stubs.
