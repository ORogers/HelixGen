"""Settings the workspace reads but does not show.

These used to sit in the prompt panel, which meant the main screen carried
backend plumbing next to the one field that actually describes a tone. They
live here instead, edited on their own page, so the workspace only shows what
a person uses on every run.

The LLM defaults come from ``hlxgen.llm``, the same constants ``hlxgen
describe`` uses, so the UI behaves like the CLI out of the box.
"""

from __future__ import annotations

from dataclasses import dataclass

from hlxgen.llm import (
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_REASONING_EFFORT,
)

#: How many slots a refresh sweeps. Each one costs a document read, so this is
#: a trade between how much of the pedal you see and how long Refresh takes.
DEFAULT_SLOT_COUNT = 16

BACKENDS: tuple[tuple[str, str], ...] = (
    ("Ollama (local)", "ollama"),
    ("OpenAI", "openai"),
)


@dataclass
class Settings:
    """Mutable, in-memory only - nothing is persisted between runs yet."""

    backend: str = "ollama"
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    openai_model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    num_ctx: int = DEFAULT_NUM_CTX
    slot_count: int = DEFAULT_SLOT_COUNT
