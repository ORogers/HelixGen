"""Join this project's model catalog onto the device's ordinal addressing.

``helix_model_information.json`` names things; the wire numbers them. This module
is the bridge, and :func:`audit_catalog` is how you find out how well the two sides
actually line up before any of it is trusted against hardware.

The join key is a model's ``internal_model_name`` (the host symbol, e.g.
``HD2_TremoloHarmonic``) plus the ``Mono``/``Stereo`` suffix the device adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..dataset import ModelCatalog, ModelDefinition
from .symbols import DeviceSymbol, DeviceSymbols, normalize_parameter_name


class ResolutionError(RuntimeError):
    """Raised when a catalog model cannot be placed on the device's symbol table."""


@dataclass(frozen=True)
class ResolvedModel:
    """A catalog model located in the device's symbol table."""

    model: ModelDefinition
    symbol: DeviceSymbol
    #: The variant suffix that was chosen: ``""``, ``"Mono"`` or ``"Stereo"``.
    variant: str

    @property
    def index(self) -> int:
        """The number a preset stores for this model."""
        return self.symbol.index

    @property
    def device_parameters(self) -> tuple[str, ...]:
        """Parameter names in the device's order."""
        return self.symbol.parameters

    def ordinal_of(self, parameter_name: str) -> int | None:
        """Position of a catalog parameter in the device's value vector."""
        return self.symbol.ordinal_of(parameter_name)

    def value_vector(self, values: dict[str, Any]) -> list[Any | None]:
        """Lay named parameter values out in device order.

        Entries the caller did not supply come back as ``None`` so a caller can tell
        "not set" from a real value, rather than silently shifting later parameters.
        """
        by_name = {normalize_parameter_name(k): v for k, v in values.items()}
        return [by_name.get(normalize_parameter_name(p)) for p in self.device_parameters]


def host_parameter_names(model: ModelDefinition) -> list[str]:
    """Catalog parameter names for a model, excluding the ``@`` pseudo-parameters."""
    return [name for name in model.parameter_names() if not name.startswith("@")]


def resolve_model(
    model: ModelDefinition,
    symbols: DeviceSymbols,
    *,
    stereo: bool | None = None,
) -> ResolvedModel:
    """Locate a catalog model in the device's symbol table.

    Raises :class:`ResolutionError` when the device has no symbol for it.
    """
    host_symbol = model.internal_name
    entry = symbols.resolve(host_symbol, stereo=stereo)
    if entry is None:
        raise ResolutionError(
            f"No device symbol for '{model.display_name}' ({host_symbol}). "
            "Either the symbol table is from a different device family, or this "
            "model is host-side only."
        )
    variant = entry.symbol[len(host_symbol) :]
    return ResolvedModel(model=model, symbol=entry, variant=variant)


@dataclass
class ModelAudit:
    """How one catalog model lines up against the device's symbol table."""

    display_name: str
    internal_name: str
    category: str | None
    variants: list[str] = field(default_factory=list)
    host_parameter_count: int = 0
    #: Per chosen variant: the device's parameter count.
    device_parameter_counts: dict[str, int] = field(default_factory=dict)
    #: Catalog parameters with no counterpart in a given variant's order.
    missing_on_device: dict[str, list[str]] = field(default_factory=dict)
    #: Device parameters with no counterpart in the catalog, per variant.
    missing_in_catalog: dict[str, list[str]] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return bool(self.variants)

    @property
    def split_by_variant(self) -> bool:
        """True where the device offers both a mono and a stereo symbol."""
        return len([v for v in self.variants if v]) > 1

    @property
    def clean(self) -> bool:
        """True where every variant's parameter list reconciles with the catalog."""
        if not self.resolved:
            return False
        return not any(self.missing_on_device.values()) and not any(
            self.missing_in_catalog.values()
        )


@dataclass
class AuditReport:
    """The result of reconciling the whole catalog against ``Helix.sym``."""

    symbol_count: int
    catalog_count: int
    models: list[ModelAudit] = field(default_factory=list)

    @property
    def resolved(self) -> list[ModelAudit]:
        return [m for m in self.models if m.resolved]

    @property
    def unresolved(self) -> list[ModelAudit]:
        return [m for m in self.models if not m.resolved]

    @property
    def split_models(self) -> list[ModelAudit]:
        return [m for m in self.models if m.split_by_variant]

    @property
    def mismatched(self) -> list[ModelAudit]:
        return [m for m in self.resolved if not m.clean]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable summary, for pasting into an issue."""
        return {
            "symbol_count": self.symbol_count,
            "catalog_count": self.catalog_count,
            "resolved": len(self.resolved),
            "unresolved": len(self.unresolved),
            "split_by_variant": len(self.split_models),
            "mismatched": len(self.mismatched),
            "unresolved_models": [
                {"model": m.display_name, "internal": m.internal_name, "category": m.category}
                for m in self.unresolved
            ],
            "mismatched_models": [
                {
                    "model": m.display_name,
                    "internal": m.internal_name,
                    "host_parameters": m.host_parameter_count,
                    "device_parameters": m.device_parameter_counts,
                    "missing_on_device": m.missing_on_device,
                    "missing_in_catalog": m.missing_in_catalog,
                }
                for m in self.mismatched
            ],
        }


def audit_model(model: ModelDefinition, symbols: DeviceSymbols) -> ModelAudit:
    """Reconcile one catalog model against every device variant of it."""
    host_names = host_parameter_names(model)
    audit = ModelAudit(
        display_name=model.display_name,
        internal_name=model.internal_name,
        category=model.category,
        variants=symbols.variants_of(model.internal_name),
        host_parameter_count=len(host_names),
    )

    host_normalized = {normalize_parameter_name(n): n for n in host_names}
    for variant in audit.variants:
        entry = symbols.by_symbol(f"{model.internal_name}{variant}")
        if entry is None:
            continue
        label = variant or "(none)"
        audit.device_parameter_counts[label] = len(entry.parameters)

        device_normalized = {normalize_parameter_name(p): p for p in entry.parameters}
        audit.missing_on_device[label] = [
            original
            for key, original in host_normalized.items()
            if key not in device_normalized
        ]
        audit.missing_in_catalog[label] = [
            original
            for key, original in device_normalized.items()
            if key not in host_normalized
        ]
    return audit


def audit_catalog(catalog: ModelCatalog, symbols: DeviceSymbols) -> AuditReport:
    """Reconcile the whole catalog against ``Helix.sym``.

    This is the cheap check that answers, in one run, whether the name-to-ordinal
    join holds across every model rather than across the handful anyone spot-checks
    by hand.
    """
    models = sorted(catalog.models(), key=lambda m: m.display_name.lower())
    report = AuditReport(symbol_count=len(symbols), catalog_count=len(models))
    report.models = [audit_model(model, symbols) for model in models]
    return report
