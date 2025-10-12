from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class ModelCatalogError(RuntimeError):
    """Raised when the model dataset cannot be loaded or queried."""


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    value_type: Optional[int]
    min_value: Optional[float]
    max_value: Optional[float]
    default: Optional[Any]
    display_type: Optional[str]
    forward_map: Dict[str, Any]
    reverse_map: Dict[str, Any]
    raw: Dict[str, Any]

    def has_default(self) -> bool:
        return self.default is not None

    def default_value(self) -> Any:
        if self.default is not None:
            return self.normalize(self.default)
        if self.value_type == 2 and self.reverse_map:
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
        """Validate and normalize an input value for the preset payload."""
        if self.value_type == 1:
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
        if self.value_type == 2:
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
            if str(numeric) not in self.forward_map:
                raise ModelCatalogError(
                    f"Value {numeric} out of range for parameter '{self.name}'"
                )
            return numeric
        return value


@dataclass(frozen=True)
class ModelDefinition:
    display_name: str
    internal_name: str
    category: Optional[str]
    based_on: Optional[str]
    parameters: Dict[str, ParameterDefinition]
    raw: Dict[str, Any]

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
        self._models_by_display: Dict[str, ModelDefinition] = {}
        self._models_by_internal: Dict[str, ModelDefinition] = {}
        self._models: List[ModelDefinition] = []
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
