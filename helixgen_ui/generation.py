"""Tone -> preset orchestration for the UI.

This deliberately does **not** shell out to ``helixgen`` as a subprocess and
does **not** call ``helixgen.cli._generate_from_chain`` either - that function
prints its result to stdout/stderr and returns a bare exit code, which is
exactly the kind of thing a UI would otherwise have to scrape. Instead this
module calls the same underlying pieces the CLI's ``describe`` command calls
(:func:`helixgen.llm.generate_chain_from_prompt`,
:func:`helixgen.generator.generate_preset`,
:class:`helixgen.validator.PresetValidator`) directly, so callers get a
structured :class:`GenerationResult` and typed exceptions instead of text.

The one thing intentionally reused rather than duplicated is *where a
described preset's default output path comes from* - ``helixgen/cli.py``'s
``_default_describe_output``/``_slugify`` - so the UI and the CLI can never
silently drift apart on that naming rule.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from helixgen.cli import DEFAULT_DATASET, DEFAULT_SCHEMA, DEFAULT_TEMPLATE_PATH
from helixgen.cli import _default_describe_output as default_describe_output
from helixgen.dataset import ModelCatalog
from helixgen.generator import generate_preset
from helixgen.io import load_json_file
from helixgen.llm import (
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_REASONING_EFFORT,
    LLMGenerationError,
    generate_chain_from_prompt,
)
from helixgen.validator import PresetValidator

__all__ = [
    "GenerationOptions",
    "GenerationResult",
    "GenerationValidationError",
    "LLMGenerationError",
    "generate_tone",
]


class GenerationValidationError(RuntimeError):
    """Raised when a generated preset fails structural/semantic validation.

    Carries the individual error strings (the same ones the CLI prints one per
    line under "Validation errors:") so the UI can list them instead of just
    showing a summary.
    """

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors) or "Preset failed validation")
        self.errors = errors


@dataclass
class GenerationOptions:
    """Everything needed to turn a tone prompt into a written ``.hlx`` file.

    The LLM defaults are ``helixgen.llm``'s, the same ones ``helixgen describe``
    uses, so the UI's out-of-the-box behaviour matches the CLI.
    """

    prompt: str
    dataset: Path = DEFAULT_DATASET
    template: Path = DEFAULT_TEMPLATE_PATH
    schema: Path = DEFAULT_SCHEMA
    backend: str = "ollama"
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    openai_model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    num_ctx: int = DEFAULT_NUM_CTX
    output: Path | None = None
    name: str | None = None
    author: str | None = None


@dataclass
class GenerationResult:
    chain: dict[str, Any]
    preset: dict[str, Any]
    output_path: Path
    warnings: list[str] = field(default_factory=list)


def generate_tone(
    options: GenerationOptions,
    on_progress: Callable[[str], None] | None = None,
) -> GenerationResult:
    """Run prompt -> chain -> preset -> validate -> write, reporting progress.

    Raises :class:`~helixgen.llm.LLMGenerationError` if the LLM round trips
    fail, :class:`GenerationValidationError` if the assembled preset doesn't
    pass the same gate the CLI enforces, or :class:`~helixgen.dataset.ModelCatalogError`
    / OSError for input-loading problems. Callers (the Qt worker) are expected
    to catch these and turn them into a status message - this function itself
    never touches stdout/stderr or a Qt object.
    """
    catalog = ModelCatalog(options.dataset)
    template = load_json_file(options.template)

    chain = generate_chain_from_prompt(
        prompt=options.prompt,
        catalog=catalog,
        llm_model=options.ollama_model,
        endpoint=options.ollama_endpoint,
        backend=options.backend,
        openai_model=options.openai_model,
        reasoning_effort=options.reasoning_effort,
        num_ctx=options.num_ctx,
        on_progress=on_progress,
    )

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    _report("Assembling preset from template and catalog defaults...")
    preset, report = generate_preset(
        chain=chain,
        catalog=catalog,
        template=template,
        overrides={"name": options.name, "author": options.author},
    )

    _report("Validating preset...")
    validator = PresetValidator(options.schema, catalog)
    errors = validator.validate_structural(preset)
    errors.extend(validator.validate_semantic(preset))
    if errors:
        raise GenerationValidationError(errors)

    output_path = options.output or default_describe_output(preset)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(preset, fh, indent=2)
        fh.write("\n")
    _report(f"Wrote {output_path}")

    return GenerationResult(
        chain=chain,
        preset=preset,
        output_path=output_path,
        warnings=list(report.warnings),
    )
