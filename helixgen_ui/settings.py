"""Settings the workspace reads but does not show.

These used to sit in the prompt panel, which meant the main screen carried
backend plumbing next to the one field that actually describes a tone. They
live here instead, edited on their own page, so the workspace only shows what
a person uses on every run.

The LLM defaults come from ``helixgen.llm``, the same constants ``helixgen
describe`` uses, so the UI behaves like the CLI out of the box.

Everything here is **persisted** to ``helixgen.config``'s file, so a choice made
once survives a restart. The OpenAI key is not part of this dataclass: it is
read and written through ``helixgen.llm`` so that the CLI resolves the same key
from the same place, and so it is never held in an object that gets logged or
passed around.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from helixgen.config import read_settings, write_settings
from helixgen.llm import (
    DEFAULT_BACKEND,
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_REASONING_EFFORT,
)

#: How many slots a refresh sweeps. Each one costs a document read, so this is
#: a trade between how much of the pedal you see and how long Refresh takes.
DEFAULT_SLOT_COUNT = 16

#: Backends, in the order they are offered. OpenAI leads because it is the
#: better default: faster, and better chains. Ollama is the offline, free,
#: nothing-leaves-this-machine alternative.
BACKENDS: tuple[tuple[str, str], ...] = (
    ("OpenAI", "openai"),
    ("Ollama (local)", "ollama"),
)

#: Prefix for this dataclass's keys in the shared config file, so settings
#: cannot collide with anything else remembered there.
_PREFIX = "ui."


@dataclass
class Settings:
    """What the settings page edits. Saved to disk on every change."""

    backend: str = DEFAULT_BACKEND
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    ollama_endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    openai_model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    num_ctx: int = DEFAULT_NUM_CTX
    slot_count: int = DEFAULT_SLOT_COUNT

    @classmethod
    def load(cls) -> Settings:
        """Read what was saved, falling back to a default per field.

        Each field is validated on its own type, so an edited or outdated
        config file costs the one setting it got wrong rather than resetting
        everything a person had chosen.
        """
        stored = read_settings()
        values = {}
        for field in fields(cls):
            saved = stored.get(_PREFIX + field.name)
            # Compared against the default's type rather than the annotation,
            # which is a string here thanks to `from __future__ import
            # annotations`. bool is excluded explicitly because it passes as
            # an int and would silently become a context window of 1.
            wanted = type(field.default)
            if isinstance(saved, bool) or not isinstance(saved, wanted):
                continue
            values[field.name] = saved
        return cls(**values)

    def save(self) -> None:
        write_settings({_PREFIX + key: value for key, value in asdict(self).items()})
