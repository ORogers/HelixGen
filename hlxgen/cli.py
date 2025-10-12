from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from .dataset import ModelCatalog, ModelCatalogError
from .generator import DEFAULT_TEMPLATE, generate_preset
from .inspector import inspect_preset
from .io import load_chain_spec, load_json_file
from .validator import PresetValidator, ValidationError

DEFAULT_DATASET = Path("helix_model_information.json")
DEFAULT_SCHEMA = Path("helix-preset.schema.json")
DEFAULT_TEMPLATE_PATH = Path(DEFAULT_TEMPLATE)


def _path(path_like: str) -> Path:
    return Path(path_like).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hlxgen",
        description="Generate, validate and inspect Line 6 Helix (.hlx) preset files.",
    )
    parser.add_argument(
        "--dataset",
        type=_path,
        default=DEFAULT_DATASET,
        help="Path to helix_model_information.json (default: ./helix_model_information.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate a .hlx preset from a chain specification",
    )
    generate_parser.add_argument(
        "chain",
        type=_path,
        help="Path to the signal chain definition (JSON or YAML)",
    )
    generate_parser.add_argument(
        "--name",
        help="Override preset name",
    )
    generate_parser.add_argument("--author", help="Preset author metadata")
    generate_parser.add_argument(
        "--tempo",
        type=float,
        help="Global tempo (BPM) override",
    )
    generate_parser.add_argument(
        "--device",
        help="Device string to embed into preset metadata",
    )
    generate_parser.add_argument(
        "--output",
        type=_path,
        help="Destination .hlx path (defaults to <chain-name>.hlx)",
    )
    generate_parser.add_argument(
        "--device-id",
        type=int,
        help="Numeric device identifier to embed (default mimics HX Stomp)",
    )
    generate_parser.add_argument(
        "--device-version",
        type=int,
        help="Firmware/device version integer (default mimics HX Stomp)",
    )
    generate_parser.add_argument(
        "--app-version",
        type=int,
        help="Application version integer stored in preset metadata",
    )
    generate_parser.add_argument(
        "--schema",
        type=_path,
        default=DEFAULT_SCHEMA,
        help="JSON schema used to validate the generated preset before writing",
    )
    generate_parser.add_argument(
        "--template",
        type=_path,
        default=DEFAULT_TEMPLATE_PATH,
        help="Template .hlx file used as a base when generating presets",
    )
    generate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate but do not write output file",
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate an existing .hlx file",
    )
    validate_parser.add_argument("preset", type=_path, help="Path to the .hlx file")
    validate_parser.add_argument(
        "--schema",
        type=_path,
        default=DEFAULT_SCHEMA,
        help="JSON schema used for structural validation",
    )
    validate_parser.add_argument(
        "--report",
        type=_path,
        help="Optional path to write a JSON validation report",
    )

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect a preset file and print a summary table",
    )
    inspect_parser.add_argument("preset", type=_path, help="Path to the .hlx file")
    inspect_parser.add_argument(
        "--dataset",
        dest="inspect_dataset",
        type=_path,
        default=None,
        help="Optional alternate dataset path (defaults to --dataset value)",
    )

    models_parser = subparsers.add_parser(
        "models",
        help="List available models from the dataset",
    )
    models_parser.add_argument(
        "--category",
        help="Filter models by category (case-insensitive)",
    )

    return parser


def run_generate(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    chain = load_chain_spec(args.chain)
    template = load_json_file(args.template)
    preset, report = generate_preset(
        chain=chain,
        catalog=catalog,
        template=template,
        overrides={
            "name": args.name,
            "author": args.author,
            "tempo": args.tempo,
            "device": args.device,
            "device_id": args.device_id,
            "device_version": args.device_version,
            "app_version": args.app_version,
        },
    )
    for warning in report.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    validator = PresetValidator(args.schema, catalog)
    errors = validator.validate_structural(preset)
    errors.extend(validator.validate_semantic(preset))

    if errors:
        _emit_errors(errors)
        return 1

    if args.dry_run:
        print(json.dumps(preset, indent=2))
        return 0

    output_path = args.output
    if output_path is None:
        output_path = args.chain.with_suffix(".hlx")

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(preset, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {output_path}")
    return 0


def run_validate(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    preset = load_json_file(args.preset)
    validator = PresetValidator(args.schema, catalog)
    errors = validator.validate_structural(preset)
    errors.extend(validator.validate_semantic(preset))
    result = {"preset": str(args.preset), "valid": not errors, "errors": errors}
    if args.report:
        with args.report.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
    if errors:
        _emit_errors(errors)
        return 1
    print(f"{args.preset} is valid.")
    return 0


def run_inspect(args: argparse.Namespace) -> int:
    dataset_path = args.inspect_dataset or args.dataset
    catalog = ModelCatalog(dataset_path)
    preset = load_json_file(args.preset)
    table = inspect_preset(preset, catalog)
    print(table)
    return 0


def run_models(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    rows: list[tuple[str, str, str]] = []
    category_filter = args.category.lower() if args.category else None

    for model in sorted(catalog.models(), key=lambda m: m.display_name.lower()):
        category = model.category or ""
        if category_filter and category.lower() != category_filter:
            continue
        rows.append(
            (
                model.display_name,
                model.internal_name,
                model.based_on or "",
            )
        )

    if not rows:
        print("No models found.")
        return 0

    headers = ("Model", "Internal ID", "Based On")
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def format_row(row: tuple[str, str, str]) -> str:
        return " │ ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    header_line = format_row(headers)
    separator = "─┼─".join("─" * width for width in widths)
    lines = [
        f"┌{'┬'.join('─' * width for width in widths)}┐",
        f"│{header_line}│",
        f"├{separator}┤",
    ]
    for row in rows:
        lines.append(f"│{format_row(row)}│")
    lines.append(f"└{'┴'.join('─' * width for width in widths)}┘")
    print("\n".join(lines))
    return 0


def _emit_errors(errors: list[Any]) -> None:
    if not errors:
        return
    print("Validation errors:", file=sys.stderr)
    for error in errors:
        print(f" - {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return run_generate(args)
        if args.command == "validate":
            return run_validate(args)
        if args.command == "inspect":
            return run_inspect(args)
        if args.command == "models":
            return run_models(args)
    except ModelCatalogError as exc:
        parser.error(str(exc))
    except ValidationError as exc:
        parser.error(str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
