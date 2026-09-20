import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .dataset import ModelCatalog, ModelCatalogError
from .device import AuditReport, DeviceSymbols, SymbolsError, audit_catalog
from .generator import DEFAULT_TEMPLATE, generate_preset
from .inspector import inspect_preset
from .io import load_chain_spec, load_json_file
from .llm import (
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_REASONING_EFFORT,
    OPENAI_MODELS,
    REASONING_EFFORTS,
    LLMGenerationError,
    generate_chain_from_prompt,
    list_llm_models,
    supported_reasoning_efforts,
)
from .validator import PresetValidator, ValidationError

DEFAULT_DATASET = Path("helix_model_information.json")
DEFAULT_SCHEMA = Path("helix-preset.schema.json")
DEFAULT_TEMPLATE_PATH = Path(DEFAULT_TEMPLATE)
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_UPLOAD_SCRIPT = REPO_ROOT / "scripts" / "import_helix_preset.applescript"


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
    _add_generation_arguments(generate_parser)

    generate_parser.add_argument(
        "--upload",
        action="store_true",
        help="Send the generated preset to the pedal after writing it",
    )
    generate_parser.add_argument(
        "--upload-via",
        dest="upload_via",
        choices=("usb", "applescript"),
        default=None,
        help=(
            "How to upload. Defaults to usb when a device is attached, and "
            "falls back to the AppleScript uploader otherwise."
        ),
    )
    generate_parser.add_argument(
        "--slot",
        dest="upload_slot",
        type=int,
        default=None,
        help="Target slot for a USB upload. Required with --upload-via usb.",
    )
    generate_parser.add_argument(
        "--upload-script",
        dest="upload_script",
        type=_path,
        default=None,
        help="Path to the AppleScript automation used with --upload",
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

    audit_parser = subparsers.add_parser(
        "device-audit",
        help="Reconcile the model catalog against an HX Edit Helix.sym symbol table",
    )
    audit_parser.add_argument(
        "--symbols",
        type=_path,
        required=True,
        help=(
            "Path to Helix.sym, copied from your own HX Edit installation. "
            "It is not distributed with this project."
        ),
    )
    audit_parser.add_argument(
        "--report",
        type=_path,
        help="Optional path to write the full audit as JSON",
    )
    audit_parser.add_argument(
        "--limit",
        type=int,
        default=15,
        help="How many models to list per problem section (default: 15)",
    )

    devices_parser = subparsers.add_parser(
        "devices",
        help="List attached Line 6 HX hardware over USB",
    )
    devices_parser.add_argument(
        "--identify",
        action="store_true",
        help="Open a control session on each device to confirm it responds",
    )

    pull_parser = subparsers.add_parser(
        "pull",
        help="Read one preset slot off the device without loading it",
    )
    pull_parser.add_argument("--slot", type=int, required=True, help="Slot to read")
    pull_parser.add_argument("--bank", type=int, default=0, help="Bank (default: 0)")
    pull_parser.add_argument(
        "-o", "--output", type=_path, required=True, help="Where to write the document"
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="Read every populated slot into a directory",
    )
    backup_parser.add_argument(
        "-o", "--output", type=_path, required=True, help="Directory to write into"
    )
    backup_parser.add_argument("--bank", type=int, default=0, help="Bank (default: 0)")
    backup_parser.add_argument(
        "--count", type=int, default=126, help="How many slots to sweep (default: 126)"
    )

    push_parser = subparsers.add_parser(
        "push",
        help="Write an .hlx preset into a chosen slot over USB",
    )
    push_parser.add_argument("preset", type=_path, help="The .hlx file to send")
    push_parser.add_argument(
        "--slot",
        type=int,
        required=True,
        help="Target slot. Required -- this overwrites what is there.",
    )
    push_parser.add_argument("--bank", type=int, default=0, help="Bank (default: 0)")
    push_parser.add_argument(
        "--symbols",
        type=_path,
        help="Path to Helix.sym (found inside HX Edit automatically if omitted)",
    )
    push_parser.add_argument(
        "--archive",
        type=_path,
        help="Write the slot's current contents here before overwriting it",
    )
    push_parser.add_argument(
        "--separate-cabs",
        action="store_true",
        help=(
            "Keep each cab in its own slot instead of fusing it into the amp. "
            "By default an amp carries its own cab, as it does on the pedal."
        ),
    )
    push_parser.add_argument(
        "--via",
        choices=("edits", "document"),
        default="edits",
        help=(
            "How to apply the preset. 'edits' (default) uses surgical ops and is "
            "the only path that can change a block's model. 'document' sends the "
            "whole preset in one op-21 write, which suits restoring a document."
        ),
    )
    push_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the document and report, but send nothing",
    )
    push_parser.add_argument(
        "-o",
        "--output",
        type=_path,
        help="With --dry-run, write the built document here",
    )
    push_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the read-back comparison after committing",
    )

    llm_models_parser = subparsers.add_parser(
        "llm-models",
        help="List the LLMs describe can use, with their thinking levels",
    )
    _add_llm_backend_arguments(llm_models_parser)

    describe_parser = subparsers.add_parser(
        "describe",
        help="Generate a preset from a tone description using Ollama or OpenAI",
    )
    describe_parser.add_argument(
        "prompt",
        help="Natural language description of the desired tone",
    )
    describe_parser.add_argument(
        "--ollama-model",
        dest="ollama_model",
        default=DEFAULT_OLLAMA_MODEL,
        help=f"Ollama model name to query (default: {DEFAULT_OLLAMA_MODEL})",
    )
    _add_llm_backend_arguments(describe_parser)
    describe_parser.add_argument(
        "--openai-model",
        dest="openai_model",
        choices=[model_id for model_id, _ in OPENAI_MODELS],
        default=DEFAULT_OPENAI_MODEL,
        help=f"OpenAI model used with --llm-backend openai (default: {DEFAULT_OPENAI_MODEL})",
    )
    describe_parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        choices=REASONING_EFFORTS,
        default=DEFAULT_REASONING_EFFORT,
        help=(
            "How long the model thinks before answering. Lower is faster "
            f"(default: {DEFAULT_REASONING_EFFORT}). Ollama models without a level "
            "use the nearest one they support."
        ),
    )
    describe_parser.add_argument(
        "--num-ctx",
        dest="num_ctx",
        type=int,
        default=DEFAULT_NUM_CTX,
        help=(
            f"Ollama context window in tokens (default: {DEFAULT_NUM_CTX}); raised "
            "automatically if a prompt would not fit"
        ),
    )
    describe_parser.add_argument(
        "--upload",
        action="store_true",
        help="Run the HX Edit AppleScript uploader after generating (macOS only)",
    )
    describe_parser.add_argument(
        "--upload-via",
        dest="upload_via",
        choices=("usb", "applescript"),
        default=None,
        help=(
            "How to upload. Defaults to usb when a device is attached, and "
            "falls back to the AppleScript uploader otherwise."
        ),
    )
    describe_parser.add_argument(
        "--slot",
        dest="upload_slot",
        type=int,
        default=None,
        help="Target slot for a USB upload. Required with --upload-via usb.",
    )
    describe_parser.add_argument(
        "--upload-script",
        dest="upload_script",
        type=_path,
        help=(
            "Path to the AppleScript automation used with --upload "
            f"(default: {DEFAULT_UPLOAD_SCRIPT})"
        ),
    )
    describe_parser.add_argument(
        "--upload-mode",
        choices=("auto", "manual"),
        default="auto",
        help=(
            "Controls how the uploader chooses the target slot. "
            "'auto' overwrites the first preset slot, 'manual' pauses so you can pick."
        ),
    )
    _add_generation_arguments(describe_parser)

    return parser


def _add_llm_backend_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-backend",
        choices=("ollama", "openai"),
        default="ollama",
        help="Select which LLM provider to use (default: ollama)",
    )
    parser.add_argument(
        "--ollama-endpoint",
        dest="ollama_endpoint",
        default=DEFAULT_OLLAMA_ENDPOINT,
        help="HTTP endpoint for the Ollama generate API",
    )


def _add_generation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        help="Override preset name",
    )
    parser.add_argument("--author", help="Preset author metadata")
    parser.add_argument(
        "--tempo",
        type=float,
        help="Global tempo (BPM) override",
    )
    parser.add_argument(
        "--device",
        help="Device string to embed into preset metadata",
    )
    parser.add_argument(
        "--output",
        type=_path,
        help="Destination .hlx path",
    )
    parser.add_argument(
        "--device-id",
        type=int,
        help="Numeric device identifier to embed (default mimics HX Stomp)",
    )
    parser.add_argument(
        "--device-version",
        type=int,
        help="Firmware/device version integer (default mimics HX Stomp)",
    )
    parser.add_argument(
        "--app-version",
        type=int,
        help="Application version integer stored in preset metadata",
    )
    parser.add_argument(
        "--schema",
        type=_path,
        default=DEFAULT_SCHEMA,
        help="JSON schema used to validate the generated preset before writing",
    )
    parser.add_argument(
        "--template",
        type=_path,
        default=DEFAULT_TEMPLATE_PATH,
        help="Template .hlx file used as a base when generating presets",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate but do not write output file",
    )


def run_generate(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    chain = load_chain_spec(args.chain)
    template = load_json_file(args.template)
    try:
        post_write = _build_upload_hook(args)
    except UploadOptionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return _generate_from_chain(
        chain,
        args,
        catalog,
        template,
        default_output=lambda preset: args.chain.with_suffix(".hlx"),
        post_write=post_write,
    )


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
    lines.extend(f"│{format_row(row)}│" for row in rows)
    lines.append(f"└{'┴'.join('─' * width for width in widths)}┘")
    print("\n".join(lines))
    return 0


def run_llm_models(args: argparse.Namespace) -> int:
    backend = args.llm_backend
    try:
        models = list_llm_models(backend, args.ollama_endpoint)
    except LLMGenerationError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 1
    if not models:
        print("No models found.")
        return 0

    descriptions = dict(OPENAI_MODELS) if backend == "openai" else {}
    default = DEFAULT_OPENAI_MODEL if backend == "openai" else DEFAULT_OLLAMA_MODEL
    width = max(len(model) for model in models)
    for model in models:
        levels = ", ".join(
            supported_reasoning_efforts(backend, model, args.ollama_endpoint)
        ) or "not supported"
        note = descriptions.get(model, "")
        if model == default:
            note = f"{note}, default" if note else "default"
        suffix = f"  ({note})" if note else ""
        print(f"{model.ljust(width)}  thinking: {levels}{suffix}")
    return 0


def run_describe(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    template = load_json_file(args.template)
    backend = getattr(args, "llm_backend", "ollama") or "ollama"
    try:
        chain = generate_chain_from_prompt(
            prompt=args.prompt,
            catalog=catalog,
            llm_model=args.ollama_model,
            endpoint=args.ollama_endpoint,
            backend=backend,
            openai_model=getattr(args, "openai_model", None),
            reasoning_effort=args.reasoning_effort,
            num_ctx=args.num_ctx,
        )
    except LLMGenerationError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 1

    try:
        post_write = _build_upload_hook(args)
    except UploadOptionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return _generate_from_chain(
        chain,
        args,
        catalog,
        template,
        default_output=_default_describe_output,
        post_write=post_write,
    )


def run_device_audit(args: argparse.Namespace) -> int:
    catalog = ModelCatalog(args.dataset)
    symbols = DeviceSymbols.load(args.symbols)
    report = audit_catalog(catalog, symbols)

    print(_format_audit(report, limit=args.limit))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
            fh.write("\n")
        print(f"\nWrote {args.report}")

    # The audit is diagnostic, not a gate: a real symbol table always carries
    # host-only models that legitimately have no device symbol, so findings are
    # reported rather than turned into a failure.
    return 0


def _format_audit(report: AuditReport, limit: int = 15) -> str:
    lines = [
        f"Symbol table:   {report.symbol_count} device symbols",
        f"Catalog:        {report.catalog_count} models",
        f"Resolved:       {len(report.resolved)}",
        f"Unresolved:     {len(report.unresolved)}",
        f"Mono/Stereo:    {len(report.split_models)} models the device splits in two",
        f"Mismatched:     {len(report.mismatched)} whose parameters do not reconcile",
    ]

    if report.unresolved:
        lines.append("")
        lines.append("Models with no device symbol:")
        for entry in report.unresolved[:limit]:
            category = f" [{entry.category}]" if entry.category else ""
            lines.append(f"  - {entry.display_name}{category} ({entry.internal_name})")
        if len(report.unresolved) > limit:
            lines.append(f"  … and {len(report.unresolved) - limit} more")

    if report.mismatched:
        lines.append("")
        lines.append("Models whose parameter lists do not reconcile:")
        for entry in report.mismatched[:limit]:
            counts = ", ".join(
                f"{variant}={count}" for variant, count in sorted(entry.device_parameter_counts.items())
            )
            lines.append(
                f"  - {entry.display_name}: catalog={entry.host_parameter_count}, device {counts}"
            )
            for variant, names in sorted(entry.missing_on_device.items()):
                if names:
                    lines.append(f"      not on device ({variant}): {', '.join(sorted(names))}")
            for variant, names in sorted(entry.missing_in_catalog.items()):
                if names:
                    lines.append(f"      not in catalog ({variant}): {', '.join(sorted(names))}")
        if len(report.mismatched) > limit:
            lines.append(f"  … and {len(report.mismatched) - limit} more")

    return "\n".join(lines)


def _emit_errors(errors: list[Any]) -> None:
    if not errors:
        return
    print("Validation errors:", file=sys.stderr)
    for error in errors:
        print(f" - {error}", file=sys.stderr)


def _generate_from_chain(
    chain: dict[str, Any],
    args: argparse.Namespace,
    catalog: ModelCatalog,
    template: dict[str, Any],
    default_output: Callable[[dict[str, Any]], Path],
    post_write: Callable[[Path], None] | None = None,
) -> int:
    preset, report = generate_preset(
        chain=chain,
        catalog=catalog,
        template=template,
        overrides={
            "name": getattr(args, "name", None),
            "author": getattr(args, "author", None),
            "tempo": getattr(args, "tempo", None),
            "device": getattr(args, "device", None),
            "device_id": getattr(args, "device_id", None),
            "device_version": getattr(args, "device_version", None),
            "app_version": getattr(args, "app_version", None),
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

    output_path = getattr(args, "output", None)
    if output_path is None:
        output_path = default_output(preset)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(preset, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {output_path}")
    if post_write:
        try:
            post_write(output_path)
        except Exception as exc:  # noqa: BLE001 - external automation, report don't crash
            print(f"Upload failed: {exc}", file=sys.stderr)
            return 1

    return 0


def _default_describe_output(preset: dict[str, Any]) -> Path:
    meta = (
        preset.get("data", {})
        if isinstance(preset, dict)
        else {}
    )
    if isinstance(meta, dict):
        meta_section = meta.get("meta", {}) if isinstance(meta.get("meta"), dict) else {}
    else:
        meta_section = {}
    raw_name = meta_section.get("name") if isinstance(meta_section, dict) else None
    if isinstance(raw_name, str):
        name = raw_name
    elif raw_name is not None:
        name = str(raw_name)
    else:
        name = "described preset"
    slug = _slugify(name)
    return Path.cwd() / "generated-presets" / f"{slug}.hlx"


def _slugify(text: str) -> str:
    safe = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(safe)
    slug = slug.strip("-") or "preset"
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


class UploadOptionError(RuntimeError):
    """Raised when the upload options do not make a usable request."""


def _build_upload_hook(args: argparse.Namespace) -> Callable[[Path], None] | None:
    """Build the ``post_write`` hook for ``--upload``, or ``None`` if not asked.

    Shared by ``generate`` and ``describe`` so the two cannot drift: the same
    flags mean the same thing whichever command you reach them from.
    """
    if not getattr(args, "upload", False):
        return None

    transport = _choose_upload_transport(getattr(args, "upload_via", None))

    if transport == "usb":
        slot = getattr(args, "upload_slot", None)
        if slot is None:
            raise UploadOptionError(
                "--upload over USB needs --slot. There is no default and no "
                "first-free-slot guessing: the upload overwrites whatever is in "
                "the slot, so the target is always explicit."
            )

        def _usb_hook(output_path: Path) -> None:
            _upload_via_usb(output_path, slot=slot, dataset=args.dataset)

        return _usb_hook

    if sys.platform != "darwin":
        raise UploadOptionError("The AppleScript uploader is only supported on macOS.")

    script_path = getattr(args, "upload_script", None) or DEFAULT_UPLOAD_SCRIPT
    upload_mode = (getattr(args, "upload_mode", "auto") or "auto").lower()

    def _script_hook(output_path: Path) -> None:
        _upload_via_script(script_path, output_path, upload_mode)
        print(f"Triggered HX Edit upload for {output_path} (mode: {upload_mode})")

    return _script_hook


def _choose_upload_transport(requested: str | None) -> str:
    """Pick between the USB client and the AppleScript uploader.

    USB is preferred when a device is attached: it is verifiable, it can target
    a chosen slot, and it does not need HX Edit. The AppleScript path stays as a
    documented fallback rather than being deleted -- it is the only option when
    pyusb is not installed or no device is plugged in.
    """
    if requested is not None:
        return requested
    try:
        from .device.usb import find_devices

        if find_devices():
            return "usb"
    except Exception:  # noqa: BLE001 - no USB support just means the fallback
        pass
    return "applescript"


def _upload_via_usb(
    preset_path: Path, *, slot: int, bank: int = 0, dataset: Path | None = None
) -> None:
    """Send a generated preset straight to the pedal.

    Uses the surgical edit path, which is the only one that can change a block's
    model -- exactly what a freshly generated preset does.
    """
    from .dataset import ModelCatalog
    from .device.commands import find_symbol_table
    from .device.editor import apply_tone
    from .device.symbols import DeviceSymbols

    preset = json.loads(Path(preset_path).read_text())
    symbols = DeviceSymbols.load(find_symbol_table(None))
    name = preset.get("data", {}).get("meta", {}).get("name") or Path(preset_path).stem
    catalog = ModelCatalog(dataset) if dataset else None

    from .device.commands import _load_amp_defaults

    report = apply_tone(
        preset,
        symbols,
        bank=bank,
        slot=slot,
        name=name,
        catalog=catalog,
        amps=_load_amp_defaults(),
    )
    print(report.summary())
    print(f"Uploaded {preset_path} to slot {slot} over USB.")


def _upload_via_script(
    script_path: Path,
    preset_path: Path,
    mode: str = "auto",
) -> None:
    script_path = Path(script_path)
    if not script_path.exists():
        raise FileNotFoundError(
            f"Upload script not found at {script_path}. "
            "Use --upload-script to supply the correct path."
        )
    osascript = shutil.which("osascript")
    if osascript is None:
        raise RuntimeError("osascript not found on PATH; cannot run upload script.")
    normalized_mode = (mode or "auto").lower()
    subprocess.run(
        [osascript, str(script_path), str(Path(preset_path)), normalized_mode],
        check=True,
    )


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
        if args.command == "describe":
            return run_describe(args)
        if args.command == "llm-models":
            return run_llm_models(args)
        if args.command == "device-audit":
            return run_device_audit(args)
        if args.command in {"devices", "pull", "backup", "push"}:
            from .device.commands import run_backup, run_devices, run_pull, run_push

            handlers = {
                "devices": run_devices,
                "pull": run_pull,
                "backup": run_backup,
                "push": run_push,
            }
            return handlers[args.command](args)
    except ModelCatalogError as exc:
        parser.error(str(exc))
    except SymbolsError as exc:
        parser.error(str(exc))
    except ValidationError as exc:
        parser.error(str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
