import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset import ModelCatalog, ModelCatalogError, ModelDefinition


class ValidationError(RuntimeError):
    """Raised when validation cannot be performed (missing inputs etc.)."""


@dataclass
class SchemaProblem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SimpleSchemaValidator:
    """Very small subset of JSON Schema validation needed for this project."""

    def __init__(self, schema: dict[str, Any]):
        self.schema = schema

    def validate(self, instance: Any) -> list[SchemaProblem]:
        return self._validate(self.schema, instance, "$")

    def _validate(self, schema: dict[str, Any], instance: Any, path: str) -> list[SchemaProblem]:
        problems: list[SchemaProblem] = []
        schema_type = schema.get("type")
        if schema_type:
            if isinstance(schema_type, list):
                if not any(self._is_type(instance, t) for t in schema_type):
                    problems.append(
                        SchemaProblem(path, f"Expected type {schema_type}, got {type(instance).__name__}")
                    )
                    return problems
            else:
                if not self._is_type(instance, schema_type):
                    problems.append(
                        SchemaProblem(path, f"Expected type {schema_type}, got {type(instance).__name__}")
                    )
                    return problems

        if isinstance(instance, dict):
            problems.extend(self._validate_object(schema, instance, path))
        elif isinstance(instance, list):
            problems.extend(self._validate_array(schema, instance, path))
        return problems

    def _validate_object(self, schema: dict[str, Any], instance: dict[str, Any], path: str) -> list[SchemaProblem]:
        problems: list[SchemaProblem] = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)

        problems.extend(
            SchemaProblem(path, f"Missing required property '{req}'")
            for req in required
            if req not in instance
        )

        for key, value in instance.items():
            child_path = f"{path}.{key}"
            if key in properties:
                sub_schema = properties[key]
                if isinstance(sub_schema, dict):
                    problems.extend(self._validate(sub_schema, value, child_path))
            else:
                if isinstance(additional, dict):
                    problems.extend(self._validate(additional, value, child_path))
                elif not additional:
                    problems.append(SchemaProblem(child_path, "Additional properties not allowed"))
        return problems

    def _validate_array(self, schema: dict[str, Any], instance: list[Any], path: str) -> list[SchemaProblem]:
        problems: list[SchemaProblem] = []
        items = schema.get("items")
        if isinstance(items, dict):
            for index, value in enumerate(instance):
                child_path = f"{path}[{index}]"
                problems.extend(self._validate(items, value, child_path))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            problems.append(SchemaProblem(path, f"Expected at least {schema['minItems']} items"))
        return problems

    @staticmethod
    def _is_type(instance: Any, schema_type: str) -> bool:
        mapping = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "boolean": bool,
            "integer": int,
        }
        python_type = mapping.get(schema_type)
        if not python_type:
            return True
        if schema_type == "number" and isinstance(instance, bool):
            # bool is subclass of int, ensure we do not treat bool as number
            return False
        if isinstance(python_type, tuple):
            return isinstance(instance, python_type)
        return isinstance(instance, python_type)


class PresetValidator:
    def __init__(self, schema_path: Path, catalog: ModelCatalog):
        self.schema_path = schema_path
        self.catalog = catalog
        self._schema_validator = SimpleSchemaValidator(self._load_schema())

    def _load_schema(self) -> dict[str, Any]:
        if not self.schema_path.exists():
            raise ValidationError(
                f"Schema file not found at {self.schema_path}. "
                "Ensure helix-preset.schema.json exists."
            )
        with self.schema_path.open("r", encoding="utf-8") as fh:
            try:
                schema = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid JSON schema {self.schema_path}: {exc}") from exc
        if not isinstance(schema, dict):
            raise ValidationError("Schema root must be an object.")
        return schema

    def validate_structural(self, preset: dict[str, Any]) -> list[str]:
        problems = self._schema_validator.validate(preset)
        return [str(problem) for problem in problems]

    def validate_semantic(self, preset: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            data = preset["data"]
            tone = data["tone"]
            dsp0 = tone["dsp0"]
        except (KeyError, TypeError):
            errors.append("Preset missing expected tone.dsp0 structure.")
            return errors

        for key, block in dsp0.items():
            if key in {"inputA", "outputA"}:
                continue
            if not isinstance(block, dict):
                errors.append(f"{key}: block payload must be an object.")
                continue
            model_name = block.get("@model")
            if not model_name:
                errors.append(f"{key}: missing '@model'")
                continue
            if any(
                model_name.startswith(prefix)
                for prefix in ("HelixStomp_AppDSPFlow", "HD2_AppDSPFlow")
            ):
                # Routing and infrastructure blocks are not catalogued in the dataset.
                continue
            try:
                model = self.catalog.get(model_name)
            except ModelCatalogError as exc:
                errors.append(f"{key}: {exc}")
                continue

            for param_name, value in block.items():
                if param_name.startswith("@"):
                    continue
                if param_name not in model.parameters:
                    errors.append(
                        f"{key}: parameter '{param_name}' not valid for model '{model.display_name}'"
                    )
                    continue
                definition = model.parameters[param_name]
                try:
                    definition.normalize(value)
                except ModelCatalogError as exc:  # reuse error messaging
                    errors.append(f"{key}: {exc}")

        errors.extend(self._validate_snapshots(tone, dsp0))
        return errors

    def _validate_snapshots(self, tone: dict[str, Any], dsp0: dict[str, Any]) -> list[str]:
        """Check that snapshot bypass states and controllers point at real blocks."""
        errors: list[str] = []
        controllers = tone.get("controller", {}).get("dsp0", {})
        if not isinstance(controllers, dict):
            return ["controller.dsp0: must be an object."]
        for block_key, parameters in controllers.items():
            if block_key not in dsp0:
                errors.append(f"controller.dsp0.{block_key}: no such block in dsp0")
                continue
            model = self._catalogued_model(dsp0[block_key])
            if model is None or not isinstance(parameters, dict):
                continue
            errors.extend(
                f"controller.dsp0.{block_key}: parameter '{name}' not valid for model "
                f"'{model.display_name}'"
                for name in parameters
                if name not in model.parameters
            )

        for snapshot_key, snapshot in tone.items():
            if not snapshot_key.startswith("snapshot") or not isinstance(snapshot, dict):
                continue
            states = snapshot.get("blocks", {}).get("dsp0", {})
            for block_key, state in states.items():
                if block_key not in dsp0:
                    errors.append(f"{snapshot_key}.blocks.dsp0.{block_key}: no such block in dsp0")
                elif not isinstance(state, bool):
                    errors.append(f"{snapshot_key}.blocks.dsp0.{block_key}: must be true or false")

            values = snapshot.get("controllers", {}).get("dsp0", {})
            for block_key, parameters in values.items():
                if not isinstance(parameters, dict):
                    errors.append(f"{snapshot_key}.controllers.dsp0.{block_key}: must be an object")
                    continue
                model = self._catalogued_model(dsp0.get(block_key))
                for name, entry in parameters.items():
                    location = f"{snapshot_key}.controllers.dsp0.{block_key}.{name}"
                    if name not in controllers.get(block_key, {}):
                        errors.append(f"{location}: no matching controller assignment")
                        continue
                    if not isinstance(entry, dict) or "@value" not in entry:
                        errors.append(f"{location}: missing '@value'")
                        continue
                    if model is not None and name in model.parameters:
                        try:
                            model.parameters[name].normalize(entry["@value"])
                        except ModelCatalogError as exc:
                            errors.append(f"{location}: {exc}")
        return errors

    def _catalogued_model(self, block: Any) -> ModelDefinition | None:
        if not isinstance(block, dict):
            return None
        model_name = block.get("@model")
        if not isinstance(model_name, str) or not self.catalog.has_model(model_name):
            return None
        return self.catalog.get(model_name)
