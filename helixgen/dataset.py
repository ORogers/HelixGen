import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModelCatalogError(RuntimeError):
    """Raised when the model dataset cannot be loaded or queried."""


#: The device's own parameter types, as HX Edit's model definitions record them.
#: ``DISCRETE`` is a stepped list -- a delay's note sync, a cab's mic, an IR
#: slot. **Its option numbers do not always start at zero**: the value a preset
#: stores is the parameter's own ``min`` plus the option's position in the list,
#: so a note sync runs 1..19 and a harmoniser's interval -8..8.
DISCRETE = 0
CONTINUOUS = 1
SWITCH = 2


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    value_type: int | None
    min_value: float | None
    max_value: float | None
    default: Any | None
    display_type: str | None
    forward_map: dict[str, Any]
    reverse_map: dict[str, Any]
    raw: dict[str, Any]

    def has_default(self) -> bool:
        return self.default is not None

    def default_value(self) -> Any:
        if self.default is not None:
            return self.normalize(self.default)
        if self.value_type == SWITCH and self.reverse_map:
            # prefer a neutral entry if present
            if "False" in self.reverse_map:
                return self.normalize("False")
            if "Off" in self.reverse_map:
                return self.normalize("Off")
            # fallback: take the first entry
            first_key = next(iter(self.forward_map), None)
            if first_key is not None:
                return self.normalize(int(first_key))
        return None

    def normalize(self, value: Any) -> Any:
        """Validate and normalize an input value for the preset payload.

        The result is the number a preset stores, which for a discrete
        parameter is the device's own option number and not the option's
        position in the list. Writing the position instead lands the block on a
        neighbouring option -- a dotted eighth delay arrives as a quarter -- and
        does it silently, because the wrong number is still a valid option.
        """
        if self.value_type == CONTINUOUS:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ModelCatalogError(
                    f"Parameter '{self.name}' expects numeric value"
                ) from exc
            if self.min_value is not None:
                numeric = max(self.min_value, numeric)
            if self.max_value is not None:
                numeric = min(self.max_value, numeric)
            return numeric
        # A dataset entry that predates the device's own types carries only its
        # option map; it is stored as an option number like any other list.
        if self.value_type in (DISCRETE, SWITCH) or (
            self.value_type is None and self.reverse_map
        ):
            if self.display_type == "boolean" or self.name in {"@enabled", "@stereo"}:
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "1", "yes", "on"}:
                        return True
                    if lowered in {"false", "0", "no", "off"}:
                        return False
                    raise ModelCatalogError(
                        f"Parameter '{self.name}' expects boolean-like value"
                    )
                return bool(value)
            if isinstance(value, str) and value not in self.reverse_map:
                raise ModelCatalogError(
                    f"Unknown option '{value}' for parameter '{self.name}'"
                )
            if isinstance(value, str):
                return self.reverse_map[value]
            if isinstance(value, bool):
                numeric = 1 if value else 0
            else:
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise ModelCatalogError(
                        f"Parameter '{self.name}' expects integer enum value"
                    ) from exc
            if self.forward_map:
                if str(numeric) not in self.forward_map:
                    raise ModelCatalogError(
                        f"Value {numeric} out of range for parameter '{self.name}'"
                    )
            elif (self.min_value is not None and numeric < self.min_value) or (
                self.max_value is not None and numeric > self.max_value
            ):
                # A stepped parameter the dataset has no labels for, such as a
                # vocoder's formant slot: its range is all there is to check.
                raise ModelCatalogError(
                    f"Value {numeric} out of range for parameter '{self.name}'"
                )
            return numeric
        return value


@dataclass(frozen=True)
class ModelDefinition:
    display_name: str
    internal_name: str
    category: str | None
    based_on: str | None
    parameters: dict[str, ParameterDefinition]
    raw: dict[str, Any]

    def parameter_names(self) -> Iterable[str]:
        return self.parameters.keys()

    def get_parameter(self, name: str) -> ParameterDefinition:
        try:
            return self.parameters[name]
        except KeyError as exc:
            raise ModelCatalogError(
                f"Model '{self.display_name}' has no parameter named '{name}'"
            ) from exc


class ModelCatalog:
    def __init__(self, dataset_path: Path):
        self.dataset_path = dataset_path
        self._models_by_display: dict[str, ModelDefinition] = {}
        self._models_by_internal: dict[str, ModelDefinition] = {}
        self._models: list[ModelDefinition] = []
        self._load()

    def _load(self) -> None:
        if not self.dataset_path.exists():
            raise ModelCatalogError(
                f"Dataset not found at {self.dataset_path}. "
                "Provide --dataset to point to helix_model_information.json"
            )
        with self.dataset_path.open("r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ModelCatalogError(
                    f"Failed to decode JSON dataset {self.dataset_path}: {exc}"
                ) from exc
        if not isinstance(data, dict):
            raise ModelCatalogError(
                f"Expected top-level object in dataset {self.dataset_path}"
            )

        for display_name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            internal_name = entry.get("internal_model_name") or entry.get("model")
            if not internal_name:
                continue
            parameters = {}
            for param_name, param_entry in entry.get("parameters", {}).items():
                parameters[param_name] = ParameterDefinition(
                    name=param_name,
                    value_type=param_entry.get("valueType"),
                    min_value=param_entry.get("min"),
                    max_value=param_entry.get("max"),
                    default=param_entry.get("default"),
                    display_type=param_entry.get("displayType"),
                    forward_map=param_entry.get("forwardMap", {}),
                    reverse_map=param_entry.get("reverseMap", {}),
                    raw=param_entry,
                )
            model = ModelDefinition(
                display_name=display_name,
                internal_name=internal_name,
                category=entry.get("category"),
                based_on=entry.get("basedOn"),
                parameters=parameters,
                raw=entry,
            )
            self._models_by_display[display_name.lower()] = model
            self._models_by_internal[internal_name.lower()] = model
            if entry.get("model"):
                self._models_by_display[entry["model"].lower()] = model
            self._models.append(model)

    def get(self, identifier: str) -> ModelDefinition:
        key = identifier.lower()
        if key in self._models_by_display:
            return self._models_by_display[key]
        if key in self._models_by_internal:
            return self._models_by_internal[key]
        raise ModelCatalogError(f"Model '{identifier}' not found in dataset")

    def has_model(self, identifier: str) -> bool:
        key = identifier.lower()
        return key in self._models_by_display or key in self._models_by_internal

    def models(self) -> Iterable[ModelDefinition]:
        return list(self._models)
