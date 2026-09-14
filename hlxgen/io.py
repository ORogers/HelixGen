import json
from pathlib import Path
from typing import Any

from .validator import ValidationError

YAML_EXTENSIONS = {".yaml", ".yml"}


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError(f"File not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON in {path}: {exc}") from exc


def load_chain_spec(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in YAML_EXTENSIONS:
        return _load_yaml(path)
    if suffix == ".json" or suffix == ".hlxchain":
        return load_json_file(path)
    raise ValidationError(
        f"Unsupported chain format for {path}. Use .json or .yaml/.yml files."
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise ValidationError(
            "PyYAML is required to read YAML chain specs. "
            "Install it via 'pip install pyyaml' or use JSON."
        ) from exc
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)  # type: ignore[attr-defined]
    if not isinstance(data, dict):
        raise ValidationError(f"Expected mapping at top level of {path}")
    return data
