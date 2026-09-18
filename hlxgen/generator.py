import contextlib
import copy
from dataclasses import dataclass, field
from typing import Any

from .dataset import (
    CONTINUOUS,
    ModelCatalog,
    ModelCatalogError,
    ModelDefinition,
    ParameterDefinition,
)

DEFAULT_APPLICATION = "HX Edit"
DEFAULT_APP_VERSION = 58851328  # Matches HX Edit 3.70 numeric encoding
DEFAULT_DEVICE_ID = 2162694  # HX Stomp / HX Edit identifiers observed in reference presets
DEFAULT_DEVICE_VERSION = 57671680  # HX Stomp firmware encoding (e.g. 3.60)
DEFAULT_TEMPLATE = "HXTemplate.hlx"
DEFAULT_FOOTSWITCH_LED = 13676288
# The controller source HX Edit writes for "Snapshots" on HX Stomp-family devices.
SNAPSHOT_CONTROLLER = 9
# Longest snapshot name the device displays; HX Edit truncates anything longer.
SNAPSHOT_NAME_LIMIT = 10


@dataclass
class GenerationReport:
    warnings: list[str] = field(default_factory=list)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


def generate_preset(
    chain: dict[str, Any],
    catalog: ModelCatalog,
    template: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], GenerationReport]:
    overrides = overrides or {}
    report = GenerationReport()

    preset = copy.deepcopy(template)
    data_section = preset.get("data")
    if not isinstance(data_section, dict):
        raise ModelCatalogError("Template must contain a top-level 'data' object.")
    template_meta = data_section.get("meta", {})
    template_tone = data_section.get("tone", {})
    template_dsp0 = template_tone.get("dsp0", {})
    template_global = template_tone.get("global", {})
    template_device = data_section.get("device", DEFAULT_DEVICE_ID)
    template_device_version = data_section.get("device_version", DEFAULT_DEVICE_VERSION)
    template_app_version = template_meta.get("appversion", DEFAULT_APP_VERSION)

    blocks_spec = chain.get("blocks")
    if not isinstance(blocks_spec, list) or not blocks_spec:
        raise ModelCatalogError(
            "Signal chain must define a non-empty 'blocks' list."
        )

    normalized_blocks: list[dict[str, Any]] = []
    for entry in blocks_spec:
        if isinstance(entry, dict):
            normalized_blocks.append(entry)
            continue
        if isinstance(entry, str):
            stripped = entry.strip()
            if not stripped:
                raise ModelCatalogError(
                    "Block entries must be model names or mapping objects."
                )
            normalized_blocks.append({"model": stripped})
            continue
        raise ModelCatalogError(
            "Each block must be a mapping of properties or a model name string."
        )

    blocks_spec = normalized_blocks

    chain_meta: dict[str, Any] = {}
    if isinstance(chain.get("meta"), dict):
        chain_meta.update(chain["meta"])

    title_value = chain.get("title")
    if isinstance(title_value, str) and title_value.strip():
        chain_meta.setdefault("name", title_value.strip())

    author_value = chain.get("author")
    if isinstance(author_value, str) and author_value.strip():
        chain_meta.setdefault("author", author_value.strip())

    description_value = chain.get("description")
    if isinstance(description_value, str) and description_value.strip():
        chain_meta.setdefault("description", description_value.strip())

    preset_meta = dict(template_meta)
    preset_meta.update(chain_meta)
    preset_name = overrides.get("name") or preset_meta.get("name") or "Generated Preset"
    preset_author = overrides.get("author") or preset_meta.get("author")
    device_string = overrides.get("device") or preset_meta.get("device")
    tempo_override = overrides.get("tempo")

    global_section = copy.deepcopy(template_global) if isinstance(template_global, dict) else {}
    if "@tempo" not in global_section:
        global_section["@tempo"] = 120.0
    if "@current_snapshot" not in global_section:
        global_section["@current_snapshot"] = 0
    global_spec = chain.get("global", {})
    if global_spec and not isinstance(global_spec, dict):
        raise ModelCatalogError("'global' section must be an object with key/value pairs.")
    if global_spec:
        global_section.update(global_spec)
    if tempo_override is not None:
        global_section["@tempo"] = float(tempo_override)

    template_input = template_dsp0.get("inputA", {})
    template_output_main = template_dsp0.get("outputA", {})
    template_output_send = template_dsp0.get("outputB") if isinstance(template_dsp0, dict) else None

    input_override = chain.get("input")
    if input_override is not None and not isinstance(input_override, dict):
        raise ModelCatalogError("'input' section must be an object.")
    input_default = {
        "@model": "HelixStomp_AppDSPFlowInput",
        "@input": 1,
    }
    input_block = _merge_dicts(
        template_input if isinstance(template_input, dict) else {},
        input_override if input_override is not None else input_default,
    )
    for key, value in input_default.items():
        input_block.setdefault(key, value)

    output_override = chain.get("output")
    if output_override is not None and not isinstance(output_override, dict):
        raise ModelCatalogError("'output' section must be an object.")
    output_default = {
        "@model": "HelixStomp_AppDSPFlowOutputMain",
        "@output": 1,
    }
    output_block = _merge_dicts(
        template_output_main if isinstance(template_output_main, dict) else {},
        output_override if output_override is not None else output_default,
    )
    for key, value in output_default.items():
        output_block.setdefault(key, value)

    dsp_blocks: dict[str, dict[str, Any]] = copy.deepcopy(template_dsp0) if isinstance(template_dsp0, dict) else {}
    dsp_blocks["inputA"] = input_block
    if isinstance(template_dsp0, dict) and "inputB" in template_dsp0:
        dsp_blocks["inputB"] = copy.deepcopy(template_dsp0["inputB"])

    footswitch_assignments: dict[str, dict[str, Any]] = {}
    automatic_fs_candidates: list[tuple[str, ModelDefinition, int]] = []
    block_models: list[ModelDefinition] = []

    for index, block_spec in enumerate(blocks_spec):
        if not isinstance(block_spec, dict):
            raise ModelCatalogError("Each block must be a mapping of properties.")

        model_identifier = block_spec.get("model")
        if not model_identifier:
            raise ModelCatalogError("Each block must define a 'model'.")
        model = catalog.get(model_identifier)
        block_models.append(model)
        block_id = f"block{index}"

        block_payload: dict[str, Any] = {
            "@model": model.internal_name,
            "@path": block_spec.get("path", 0),
            "@position": block_spec.get("position", index),
            "@type": block_spec.get("type", 0),
            "@enabled": block_spec.get("enabled", True),
            "@stereo": block_spec.get("stereo", False),
            "@no_snapshot_bypass": bool(block_spec.get("no_snapshot_bypass", False)),
        }

        provided_parameters = block_spec.get("parameters", {})
        if provided_parameters and not isinstance(provided_parameters, dict):
            raise ModelCatalogError(
                f"Block '{block_id}' parameters must be a mapping of name/value pairs."
            )

        # Apply dataset defaults
        for param_name, definition in model.parameters.items():
            if param_name.startswith("@"):
                continue
            if param_name in provided_parameters:
                continue
            default_value = definition.default_value()
            if default_value is not None:
                block_payload[param_name] = default_value

        # Apply user-provided parameters
        for param_name, value in provided_parameters.items():
            if param_name not in model.parameters:
                raise ModelCatalogError(
                    f"Parameter '{param_name}' not valid for model '{model.display_name}'."
                )
            block_payload[param_name] = _normalize_parameter(
                model.parameters[param_name], value, f"block '{block_id}'", report
            )

        dsp_blocks[block_id] = block_payload
        template_block = template_dsp0.get(block_id) if isinstance(template_dsp0, dict) else None
        if isinstance(template_block, dict):
            for key, value in template_block.items():
                dsp_blocks[block_id].setdefault(key, value)

        fs_index = block_spec.get("footswitch")
        if fs_index is not None:
            try:
                numeric_index = int(fs_index)
            except (TypeError, ValueError) as exc:
                raise ModelCatalogError(
                    f"Footswitch index for block '{block_id}' must be numeric."
                ) from exc
            fs_label = block_spec.get("footswitch_label") or model.display_name
            fs_enabled = bool(block_spec.get("footswitch_enabled", True))
            fs_led = block_spec.get("footswitch_led", DEFAULT_FOOTSWITCH_LED)
            fs_momentary = bool(block_spec.get("footswitch_momentary", False))
            footswitch_assignments[block_id] = {
                "@fs_label": fs_label,
                "@fs_enabled": fs_enabled,
                "@fs_index": numeric_index,
                "@fs_ledcolor": fs_led,
                "@fs_primary": True,
                "@fs_momentary": fs_momentary,
            }
        else:
            automatic_fs_candidates.append((block_id, model, index))

    if automatic_fs_candidates:
        priority_categories = {"distortion", "modulation", "delay"}
        preferred: list[tuple[str, Any, int]] = []
        fallback: list[tuple[str, Any, int]] = []
        for block_id, candidate_model, order in automatic_fs_candidates:
            category = (candidate_model.category or "").lower()
            if category in priority_categories:
                preferred.append((block_id, candidate_model, order))
            else:
                fallback.append((block_id, candidate_model, order))
        ordered = preferred + fallback
        used_indexes = {
            assignment.get("@fs_index")
            for assignment in footswitch_assignments.values()
            if isinstance(assignment, dict)
        }
        next_index = 1
        for block_id, candidate_model, _ in ordered:
            if block_id in footswitch_assignments:
                continue
            while next_index in used_indexes:
                next_index += 1
            if next_index > 3:
                break
            footswitch_assignments[block_id] = {
                "@fs_label": candidate_model.display_name,
                "@fs_enabled": True,
                "@fs_index": next_index,
                "@fs_ledcolor": DEFAULT_FOOTSWITCH_LED,
                "@fs_primary": True,
                "@fs_momentary": False,
            }
            used_indexes.add(next_index)
            next_index += 1

    dsp_blocks["outputA"] = output_block
    if isinstance(template_output_send, dict):
        dsp_blocks["outputB"] = copy.deepcopy(template_output_send)

    tone_section = copy.deepcopy(template_tone) if isinstance(template_tone, dict) else {}
    tone_section["global"] = global_section
    tone_section["dsp0"] = dsp_blocks

    if (
        "@cursor_group" in tone_section["global"]
        and tone_section["global"]["@cursor_group"] in {"", None}
        and blocks_spec
    ):
        tone_section["global"]["@cursor_group"] = "block0"

    _apply_snapshots(
        tone_section,
        chain.get("snapshots"),
        blocks_spec,
        block_models,
        report,
    )

    footswitch_section = copy.deepcopy(template_tone.get("footswitch", {})) if isinstance(template_tone, dict) else {}
    if footswitch_assignments:
        dsp_fs = footswitch_section.setdefault("dsp0", {})
        for block_id, assignment in footswitch_assignments.items():
            dsp_fs[block_id] = assignment
    if footswitch_section:
        tone_section["footswitch"] = footswitch_section

    template_default_app = template_app_version if isinstance(template_app_version, (int, float)) else DEFAULT_APP_VERSION
    template_default_device = template_device if isinstance(template_device, (int, float)) else DEFAULT_DEVICE_ID
    template_default_device_version = (
        template_device_version if isinstance(template_device_version, (int, float)) else DEFAULT_DEVICE_VERSION
    )

    app_version = _coerce_numeric(
        _first_present(
            overrides.get("app_version"),
            preset_meta.get("appversion"),
            template_app_version,
        ),
        template_default_app,
        "appversion",
    )

    device_id = _coerce_numeric(
        _first_present(
            overrides.get("device_id"),
            chain.get("device_id"),
            chain.get("deviceId"),
            chain.get("device"),
            preset_meta.get("device_id"),
            preset_meta.get("deviceId"),
            preset_meta.get("device"),
            template_device,
        ),
        template_default_device,
        "device",
    )

    device_version = _coerce_numeric(
        _first_present(
            overrides.get("device_version"),
            chain.get("device_version"),
            chain.get("deviceVersion"),
            chain.get("firmware"),
            preset_meta.get("device_version"),
            preset_meta.get("deviceVersion"),
            template_device_version,
        ),
        template_default_device_version,
        "device_version",
    )

    meta_section = copy.deepcopy(template_meta) if isinstance(template_meta, dict) else {}
    meta_section["application"] = preset_meta.get("application", meta_section.get("application", DEFAULT_APPLICATION))
    meta_section["appversion"] = app_version
    meta_section["name"] = preset_name
    if preset_author:
        meta_section["author"] = preset_author
    elif "author" in meta_section and "author" not in preset_meta:
        meta_section.pop("author", None)
    if "description" in chain_meta:
        meta_section["description"] = chain_meta["description"]
    elif "description" in meta_section and "description" not in chain_meta:
        meta_section.pop("description", None)
    if "build_sha" in preset_meta:
        meta_section["build_sha"] = preset_meta["build_sha"]
    if "modifieddate" in preset_meta:
        meta_section["modifieddate"] = preset_meta["modifieddate"]
    if device_string:
        meta_section["device"] = device_string

    data_section["device"] = device_id
    data_section["device_version"] = device_version
    data_section["meta"] = meta_section
    data_section["tone"] = tone_section

    preset["data"] = data_section

    return preset, report


@dataclass
class _SnapshotSpec:
    name: str | None
    enabled: dict[str, bool]
    parameters: dict[str, dict[str, Any]]


def _apply_snapshots(
    tone: dict[str, Any],
    snapshots_spec: Any,
    blocks_spec: list[dict[str, Any]],
    block_models: list[ModelDefinition],
    report: GenerationReport,
) -> None:
    """Write per-snapshot bypass states and parameter values into ``tone``.

    A block's own ``@enabled`` and parameter values are the baseline; a snapshot
    only records how it differs. Any parameter that one snapshot changes becomes
    snapshot-controlled, so every snapshot then stores its own value for it. The
    ``dsp0`` block values are finally synced to the current snapshot, which is
    what the device shows when the preset loads.
    """
    dsp0 = tone["dsp0"]
    block_keys = [f"block{index}" for index in range(len(blocks_spec))]
    snapshot_keys = sorted(
        (
            key
            for key, value in tone.items()
            if key.startswith("snapshot")
            and key[len("snapshot"):].isdigit()
            and isinstance(value, dict)
        ),
        key=lambda key: int(key[len("snapshot"):]),
    )
    specs = _parse_snapshot_specs(
        snapshots_spec, len(snapshot_keys), blocks_spec, block_models, report
    )

    controlled: dict[str, dict[str, ParameterDefinition]] = {}
    for spec in specs:
        for block_key, parameters in spec.parameters.items():
            model = block_models[block_keys.index(block_key)]
            for name in parameters:
                controlled.setdefault(block_key, {})[name] = model.parameters[name]

    for position, snapshot_key in enumerate(snapshot_keys):
        snapshot = tone[snapshot_key]
        spec = specs[position] if position < len(specs) else _SnapshotSpec(None, {}, {})

        dsp_map = snapshot.setdefault("blocks", {}).setdefault("dsp0", {})
        for block_key in block_keys:
            dsp_map[block_key] = spec.enabled.get(block_key, bool(dsp0[block_key]["@enabled"]))

        if controlled:
            controller_map = snapshot.setdefault("controllers", {}).setdefault("dsp0", {})
            for block_key, parameters in controlled.items():
                overrides = spec.parameters.get(block_key, {})
                for name in parameters:
                    controller_map.setdefault(block_key, {})[name] = {
                        "@fs_enabled": False,
                        "@value": overrides.get(name, dsp0[block_key][name]),
                    }

        if spec.name:
            snapshot["@name"] = spec.name
            snapshot["@custom_name"] = True

    if controlled:
        controller_section = tone.setdefault("controller", {}).setdefault("dsp0", {})
        for block_key, parameters in controlled.items():
            for name, definition in parameters.items():
                low, high = definition.controller_range()  # checked when parsing
                controller_section.setdefault(block_key, {})[name] = {
                    "@controller": SNAPSHOT_CONTROLLER,
                    "@max": high,
                    "@min": low,
                }

    current = tone.get("global", {}).get("@current_snapshot", 0)
    if isinstance(current, int) and 0 <= current < len(snapshot_keys):
        active = tone[snapshot_keys[current]]
        for block_key in block_keys:
            dsp0[block_key]["@enabled"] = active["blocks"]["dsp0"][block_key]
        for block_key, parameters in controlled.items():
            for name in parameters:
                dsp0[block_key][name] = active["controllers"]["dsp0"][block_key][name]["@value"]


def _parse_snapshot_specs(
    snapshots_spec: Any,
    available: int,
    blocks_spec: list[dict[str, Any]],
    block_models: list[ModelDefinition],
    report: GenerationReport,
) -> list[_SnapshotSpec]:
    if snapshots_spec is None:
        return []
    if not isinstance(snapshots_spec, list):
        raise ModelCatalogError("'snapshots' must be a list of snapshot definitions.")
    if len(snapshots_spec) > available:
        raise ModelCatalogError(
            f"Chain defines {len(snapshots_spec)} snapshots but the template only "
            f"provides {available}."
        )

    specs: list[_SnapshotSpec] = []
    for number, entry in enumerate(snapshots_spec, start=1):
        if not isinstance(entry, dict):
            raise ModelCatalogError(f"Snapshot {number} must be an object.")

        name = entry.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise ModelCatalogError(f"Snapshot {number} name must be a string.")
            name = name.strip()
            if len(name) > SNAPSHOT_NAME_LIMIT:
                report.add_warning(
                    f"Snapshot {number} name '{name}' truncated to "
                    f"{SNAPSHOT_NAME_LIMIT} characters."
                )
                name = name[:SNAPSHOT_NAME_LIMIT].rstrip()

        blocks = entry.get("blocks", {})
        if not isinstance(blocks, dict):
            raise ModelCatalogError(
                f"Snapshot {number} 'blocks' must map block indexes to block settings."
            )

        spec = _SnapshotSpec(name=name or None, enabled={}, parameters={})
        for raw_index, settings in blocks.items():
            index = _block_index(raw_index, len(blocks_spec), number)
            block_key = f"block{index}"
            model = block_models[index]
            if not isinstance(settings, dict):
                raise ModelCatalogError(
                    f"Snapshot {number} settings for block {index} must be an object."
                )

            if "enabled" in settings:
                enabled = settings["enabled"]
                if not isinstance(enabled, bool):
                    raise ModelCatalogError(
                        f"Snapshot {number} 'enabled' for block {index} must be true or false."
                    )
                if blocks_spec[index].get("no_snapshot_bypass"):
                    raise ModelCatalogError(
                        f"Snapshot {number} sets 'enabled' on block {index}, which is "
                        "marked no_snapshot_bypass."
                    )
                spec.enabled[block_key] = enabled

            parameters = settings.get("parameters", {})
            if not isinstance(parameters, dict):
                raise ModelCatalogError(
                    f"Snapshot {number} parameters for block {index} must be a mapping."
                )
            for param_name, value in parameters.items():
                if param_name.startswith("@") or param_name not in model.parameters:
                    raise ModelCatalogError(
                        f"Parameter '{param_name}' not valid for model '{model.display_name}'."
                    )
                definition = model.parameters[param_name]
                if definition.controller_range() is None:
                    raise ModelCatalogError(
                        f"Parameter '{param_name}' on '{model.display_name}' cannot be "
                        "controlled by snapshots."
                    )
                spec.parameters.setdefault(block_key, {})[param_name] = _normalize_parameter(
                    definition, value, f"block '{block_key}' in snapshot {number}", report
                )
        specs.append(spec)
    return specs


def _block_index(raw_index: Any, block_count: int, snapshot_number: int) -> int:
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        index = raw_index
    elif isinstance(raw_index, str) and raw_index.strip().isdigit():
        index = int(raw_index.strip())
    else:
        raise ModelCatalogError(
            f"Snapshot {snapshot_number} refers to block '{raw_index}'; use the block's "
            "zero-based index in the chain."
        )
    if not 0 <= index < block_count:
        raise ModelCatalogError(
            f"Snapshot {snapshot_number} refers to block {index}, but the chain has "
            f"{block_count} blocks."
        )
    return index


def _normalize_parameter(
    definition: ParameterDefinition,
    value: Any,
    location: str,
    report: GenerationReport,
) -> Any:
    normalized = definition.normalize(value)
    if definition.value_type == CONTINUOUS:
        original_number = None
        with contextlib.suppress(TypeError, ValueError):
            original_number = float(value)
        if original_number is not None:
            if definition.min_value is not None and original_number < definition.min_value:
                report.add_warning(
                    f"Parameter '{definition.name}' on {location} clamped to minimum."
                )
            if definition.max_value is not None and original_number > definition.max_value:
                report.add_warning(
                    f"Parameter '{definition.name}' on {location} clamped to maximum."
                )
    return normalized


def _merge_dicts(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(base) if isinstance(base, dict) else {}
    if override:
        result.update(override)
    return result


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _coerce_numeric(value: Any, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ModelCatalogError(f"{label} must be numeric, got boolean")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        if text.lower().startswith(("0x", "0X")):
            try:
                return int(text, 16)
            except ValueError as exc:
                raise ModelCatalogError(f"{label} value '{value}' is not a valid hexadecimal integer") from exc
        if text.isdigit():
            return int(text)
        return default
    raise ModelCatalogError(f"{label} must be numeric, got {type(value).__name__}")
