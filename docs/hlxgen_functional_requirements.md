# 🎛 Functional Requirements — `hlxgen`

## 1. Overview

**Purpose:**  
`hlxgen` is a command-line utility for generating and validating Line 6 **Helix (.hlx)** preset files. It allows structured, programmatic creation of Helix presets from a defined signal chain and ensures schema-compliant output compatible with **HX Edit** and **Helix Native**.

**Core capabilities:**
- Generate new `.hlx` presets from a **signal chain description**.
- Validate existing presets against the verified **Helix JSON schema**.
- Use a centralized dataset of model information (`helix_model_information.json`) to ensure correctness and realism.

---

## 2. Inputs and Data Sources

| Input | Description | Format | Required |
|--------|--------------|--------|-----------|
| **Signal Chain Definition** | Ordered list describing the intended signal path: input, effects, amps, cabs, and output. Includes parameter values and optional snapshot overrides. | JSON / YAML | ✅ |
| **Model Information Dataset (`helix_model_information.json`)** | Master catalog of available models, including:<br>• Internal Helix ID (`@model`)<br>• Human name (`displayName`)<br>• Type (`amp`, `cab`, `distortion`, etc.)<br>• Category (FX / Amp / Routing)<br>• Parameters (names, default values, min/max)<br>• Based-on hardware (e.g., “Tube Screamer 808”) | JSON | ✅ |
| **Preset Schema (`helix-preset.schema.json`)** | JSON Schema derived from real `.hlx` presets (e.g., *ArchetypeClean.hlx* and *FullRainbowClean.hlx*). Defines structure, required keys, and metadata rules. | JSON | ✅ |
| **Existing Preset (optional)** | `.hlx` file to validate or modify. | JSON | ❌ |

**Data Source Principle:**  
All model and parameter validation derives exclusively from the **Model Information Dataset**.  
If a user requests a model or parameter not present in this dataset, `hlxgen` must reject or warn, ensuring only known Helix models can be generated.

---

## 3. Functional Requirements

### FR-1 — Generate Preset (`hlxgen generate`)

**Description:**  
Generate a valid `.hlx` preset from a structured chain file using model definitions from `helix_model_information.json`.

**Inputs:**
- `--chain` or positional path to a signal chain JSON/YAML file.
- Optional flags:
  - `--name` (preset name override)
  - `--author`
  - `--tempo`
  - `--device` (e.g. `Helix Native`, `HX Stomp`)
  - `--output` (target `.hlx` path)

**Behaviour:**
1. **Load model dataset** from `helix_model_information.json`.
2. **Parse the chain specification.**
3. For each block:
   - Validate model name against dataset (`model_id` or `displayName`).
   - Retrieve parameter definitions; validate all provided parameters and clamp within allowed min/max.
   - Map to internal Helix model string (`@model`, e.g. `HD2_AmpUSDoubleVib`).
   - Populate required metadata fields:
     - `@path`, `@position`, `@type`, `@enabled`, `@stereo` (defaults allowed).
4. Construct the `"tone"` section:
   - Insert nodes in correct order (`inputA`, `block0..N`, `outputA`).
   - Include `global` metadata (`@tempo`, `@current_snapshot`).
5. Populate `"meta"` section with application info and preset metadata.
6. **Validate final structure** using `helix-preset.schema.json`.
7. **Write** `.hlx` file.

**Outputs:**
- Valid `.hlx` file that passes Helix Native/HX Edit import.
- Optional JSON validation report (`--report`).

**Dependencies:**  
Relies on `helix_model_information.json` for all available models, parameter lists, and defaults.

---

### FR-2 — Validate Preset (`hlxgen validate`)

**Description:**  
Check a `.hlx` file for schema compliance and cross-verify its models and parameters against the dataset.

**Inputs:**
- `.hlx` file path
- Optional flags:
  - `--schema` (custom schema path)
  - `--dataset` (alternate model info file)
  - `--report` (write JSON report)

**Behaviour:**
1. Parse `.hlx` as JSON.
2. Validate structure against `helix-preset.schema.json`.
3. Cross-check every block’s `@model` with entries in `helix_model_information.json`.
4. Verify all parameter names exist for the model.
5. Return summary (`VALID` / `INVALID`) and list of errors.

**Outputs:**
- Console summary (Rich-formatted)
- Optional validation report in JSON

---

### FR-3 — Inspect Preset (`hlxgen inspect`)

**Description:**  
Provide a readable summary of a `.hlx` preset: signal flow, block order, and model details (including real-world equivalents from dataset).

**Outputs:**
- Tabular display:
  ```
  ┌────┬────────────────────┬────────────┬──────────────────────────────┐
  │Pos │ Model (ID)         │ Type       │ Based On                     │
  ├────┼────────────────────┼────────────┼──────────────────────────────┤
  │0   │ HD2_AmpUSDoubleVib │ Amp        │ Fender Twin Reverb Normal    │
  │1   │ HD2_Cab4X12CaliV30 │ Cab        │ Celestion Vintage 30         │
  │2   │ HD2_ReverbGlitz    │ Reverb     │ Line 6 Plate Algorithm       │
  └────┴────────────────────┴────────────┴──────────────────────────────┘
  ```

---

## 4. Integration with Model Information Dataset

### Data Use Requirements
| ID | Requirement | Description |
|----|--------------|-------------|
| DSR-1 | **Primary Source** | The file `helix_model_information.json` is the canonical catalog of available models and their parameters. |
| DSR-2 | **Model Lookup** | Each signal chain block references a `model_id` which must exist in this dataset (case-sensitive). |
| DSR-3 | **Parameter Validation** | The generator validates parameter names and numerical ranges using entries in the dataset. |
| DSR-4 | **Default Values** | If a parameter is omitted, the tool applies its dataset default. |
| DSR-5 | **Display Data** | The inspect and summary commands may use dataset fields such as `displayName` and `basedOn` for human-readable output. |
| DSR-6 | **Extensibility** | New models can be added by updating `helix_model_information.json` without modifying code. |
| DSR-7 | **Consistency Enforcement** | On generation or validation, the dataset’s `internalModel` (e.g. `HD2_AmpUSDoubleVib`) must match the `@model` field written to the `.hlx`. |

---

## 5. Non-Functional Requirements

| ID | Requirement | Description |
|----|--------------|-------------|
| NFR-1 | **Accuracy** | The output `.hlx` must exactly match the schema observed in verified examples (Hugging Face’s *FullRainbowClean.hlx*). |
| NFR-2 | **Reliability** | All generation must pass internal schema validation before writing files. |
| NFR-3 | **Extensibility** | Updating `helix_model_information.json` must immediately expand supported models and parameters. |
| NFR-4 | **Compatibility** | Output presets must load without modification in Helix Native 3.70+ and HX Edit 3.70+. |
| NFR-5 | **Transparency** | Validation errors must clearly reference the block, model, and parameter that failed. |
| NFR-6 | **Performance** | Generation and validation must complete in <1s for presets with ≤100 blocks. |
| NFR-7 | **Portability** | Works cross-platform on macOS, Windows, Linux (Python 3.13+). |

---

## 6. Schema Integration Constraints

Generated `.hlx` files must satisfy the verified schema pattern:

```json
{
  "data": {
    "meta": {
      "application": "HX Edit",
      "appversion": "3.70",
      "name": "MyPreset"
    },
    "tone": {
      "global": { "@tempo": 120.0, "@current_snapshot": 0 },
      "dsp0": {
        "inputA": { "@model": "HelixStomp_AppDSPFlowInput", "@input": 1 },
        "block0": { "@model": "HD2_AmpUSDoubleVib", "@path": 0, "@position": 1, "Drive": 0.5 },
        "outputA": { "@model": "HelixStomp_AppDSPFlowOutputMain", "@output": 1 }
      }
    }
  }
}
```

---

## 7. Acceptance Criteria

✅ Generated presets validate against `helix-preset.schema.json`.  
✅ All block models and parameters are derived from and exist in `helix_model_information.json`.  
✅ `.hlx` files open cleanly in Helix Native / HX Edit with correct block arrangement.  
✅ `hlxgen validate` correctly detects invalid or missing model IDs.  
✅ Model updates in the dataset require no code change for recognition.
