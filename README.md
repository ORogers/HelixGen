# HelixPy — `hlxgen`

`hlxgen` is a Python command-line utility for generating, validating, and inspecting Line 6 Helix (`.hlx`) preset files. It provides a structured workflow for building presets from declarative signal-chain definitions while enforcing model and parameter correctness through a centralized dataset.

## Features
- Generate schema-compliant `.hlx` presets from JSON or YAML chain descriptions.
- Validate existing presets against a JSON schema and the bundled model catalog.
- Inspect presets and display an ordered summary of blocks, model types, and real-world references.
- Describe tones in natural language and have presets built automatically via either a local Ollama model or the OpenAI Responses API.

## Project Layout
- `hlxgen/` — Python package containing the CLI (`cli.py`) and supporting modules:
  - `dataset.py` — Loads model definitions from `helix_model_information.json`.
  - `generator.py` — Builds preset payloads from chain specs and applies defaults.
  - `validator.py` — Lightweight schema + semantic validation engine.
  - `inspector.py` — Renders readable tables summarizing preset contents.
  - `io.py` — File helpers for JSON/YAML chain input.
- `helix_model_information.json` — Canonical dataset defining available Helix models and their parameters.
- `docs/` — Functional requirements and design notes.
- `tests/` — Pytest-based unit suite covering generation, validation, CLI flows, and dataset integration.

## Usage
Install dependencies (Python 3.9+):

```bash
python3 -m pip install -r requirements.txt
```

Run the CLI directly:

```bash
python3 -m hlxgen --dataset helix_model_information.json generate path/to/chain.json --schema helix-preset.schema.json
python3 -m hlxgen --dataset helix_model_information.json validate FullRainbowClean.hlx --schema helix-preset.schema.json
python3 -m hlxgen --dataset helix_model_information.json models --category Distortion
python3 -m hlxgen inspect FullRainbowClean.hlx --dataset helix_model_information.json
python3 -m hlxgen --dataset helix_model_information.json describe "spacious worship clean" --schema helix-preset.schema.json
python3 -m hlxgen describe "tight prog metal rhythm" --llm-backend openai --openai-model gpt-4o-mini
```

- `generate` expects a chain definition with an ordered `blocks` list. Optional overrides (`--name`, `--author`, `--tempo`, `--device`, `--device-id`, `--device-version`, `--app-version`, `--template`, `--output`, `--dry-run`) adjust metadata and output behavior. Defaults mirror HX Stomp numeric IDs so the resulting preset imports cleanly in HX Edit/HX Stomp.
- Templates: generation starts from `HXTemplate.hlx` (override with `--template`) so structural metadata like snapshots, global parameters, and secondary outputs match a known-good HX Stomp export.
- Footswitches: the first three blocks in a chain are auto-assigned to footswitches 1–3 (override per-block with `footswitch`, `footswitch_label`, etc. in the chain JSON).
- `validate` checks structural schema compliance and verifies every block model + parameter against the dataset. Use `--report` to write JSON results.
- `inspect` produces a simple text table summarizing the preset signal chain.
- `models` prints a catalog of available models sourced from the dataset, optionally filtered by `--category`.

### Signal Chain Formats

`hlxgen generate` accepts two complementary specification styles:

- **Simple format** — ideal for quick experiments or LLM output. Provide a title plus an ordered list of model display names. Optional `author` and `description` fields map directly into preset metadata.

  ```json
  {
    "title": "Sparkling Clean",
    "blocks": [
      "US Double Nrm",
      "LA Studio Comp",
      "Plate Reverb"
    ]
  }
  ```

- **Full format** — mirrors the internal Helix preset structure. You can override `meta`, `global`, `input`, `output`, individual block settings, footswitch assignments, and more. Any omitted values fall back to defaults pulled from the template and dataset.

  ```json
  {
    "meta": {
      "name": "Basic Clean Patch",
      "author": "Example"
    },
    "global": { "@tempo": 120.0 },
    "input": { "@model": "HelixStomp_AppDSPFlowInput", "@input": 1 },
    "output": { "@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1 },
    "blocks": [
      {
        "model": "US Double Nrm",
        "parameters": {
          "Drive": 0.45,
          "Bass": 0.5,
          "Treble": 0.6
        }
      },
      {
        "model": "Plate Reverb",
        "parameters": {
          "Decay": 0.4,
          "Mix": 0.25
        }
      }
    ]
  }
  ```

Both formats can be expressed as JSON or YAML. During generation, simple strings are expanded into structured blocks with validated parameter defaults. See `docs/examples/` for additional templates.

### Describe Command & LLM Prompting

The `describe` subcommand converts a natural-language tone request into a preset by calling your chosen LLM backend. The helper builds a structured prompt that includes:

- The minimal JSON schema (title + ordered block names) that the LLM must satisfy.
- A template chain illustrating the required object layout.
- A set of few-shot examples that pair real user goals with valid JSON responses built from the dataset’s display names.
- A category-grouped catalog summary so the model selects only valid Helix blocks.

You can pick between two providers:

- `--llm-backend ollama` *(default)* — sends prompts to a locally hosted Ollama server via `--ollama-endpoint` and `--ollama-model`.
- `--llm-backend openai` — uses the OpenAI Responses API. Set `OPENAI_API_KEY` in your environment (it can be read automatically from a `.env` file in the project tree) and specify an `--openai-model` such as `gpt-4o-mini`.

Because the few-shot responses are drawn from actual catalog entries, the LLM sees concrete demonstrations of how to translate stylistic goals into valid block selections, which measurably improves adherence to the simplified schema. The CLI expands the returned block list into a full preset using default parameters from `helix_model_information.json`. Parameter refinement for each block also runs through the same backend, so swapping providers affects both block choice and control values.

## Testing
Unit tests are written with `pytest`:

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

Tests rely on `helix_model_information.json`; ensure it is present in the project root before running the suite.

## Requirements Reference
Functional requirements are documented in `docs/hlxgen_functional_requirements.md`. The implementation and tests align with:
- Capturing model metadata exclusively from the dataset.
- Enforcing schema validation before writing presets.
- Providing inspection output for quick signal-chain review.

## Status
The core modules, CLI wiring, and unit tests are under active development. See open issues or TODO markers in source files for remaining work, such as completing the schema definition and expanding model coverage.
