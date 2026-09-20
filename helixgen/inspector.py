from collections.abc import Sequence
from typing import Any

from .dataset import ModelCatalog, ModelCatalogError, ModelDefinition


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """A box-drawn table whose borders line up with its cells.

    Shared by ``inspect`` and ``models`` rather than written twice: the second
    copy had borders a join-width short of the rows they framed, so every
    column rule sat two characters left of the one below it.
    """
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def format_row(row: Sequence[str]) -> str:
        cells = (" " + cell.ljust(widths[idx]) + " " for idx, cell in enumerate(row))
        return "│" + "│".join(cells) + "│"

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (width + 2) for width in widths) + right

    lines = [border("┌", "┬", "┐"), format_row(headers), border("├", "┼", "┤")]
    lines.extend(format_row(row) for row in rows)
    lines.append(border("└", "┴", "┘"))
    return "\n".join(lines)


def inspect_preset(preset: dict[str, Any], catalog: ModelCatalog) -> str:
    tone = preset.get("data", {}).get("tone", {})
    dsp0 = tone.get("dsp0", {})
    rows: list[tuple[str, str, str, str]] = []

    for key, block in dsp0.items():
        if key in {"inputA", "outputA"}:
            continue
        if not isinstance(block, dict):
            continue
        position = block.get("@position")
        position_str = "?" if position is None else str(position)
        model_name = block.get("@model", "Unknown")

        try:
            model_def = catalog.get(model_name)
            model_display = model_def.internal_name
            category = model_def.category or ""
            based_on = model_def.based_on or ""
        except ModelCatalogError:
            model_display = model_name
            category = "Unknown"
            based_on = ""

        rows.append((position_str, model_display, category, based_on))

    rows.sort(key=lambda item: (int(item[0]) if item[0].isdigit() else 1_000_000))

    if not rows:
        rows.append(("-", "No blocks", "", ""))

    table = render_table(("Pos", "Model (ID)", "Type", "Based On"), rows)
    snapshots = _render_snapshots(tone, catalog)
    return f"{table}\n\nSnapshots\n{snapshots}" if snapshots else table


def _render_snapshots(tone: dict[str, Any], catalog: ModelCatalog) -> str | None:
    """Tabulate each block's bypass state and snapshot-controlled values per snapshot."""
    snapshots = [
        tone[key]
        for key in sorted(
            (key for key in tone if key.startswith("snapshot") and key[len("snapshot"):].isdigit()),
            key=lambda key: int(key[len("snapshot"):]),
        )
        if isinstance(tone[key], dict) and tone[key].get("@valid", True)
    ]
    dsp0 = tone.get("dsp0", {})
    block_keys = sorted(
        (key for key in dsp0 if key.startswith("block") and isinstance(dsp0[key], dict)),
        key=lambda key: (dsp0[key].get("@position", 0), key),
    )
    if not snapshots or not block_keys:
        return None

    controllers = tone.get("controller", {}).get("dsp0", {})
    headers = ("Block", *(str(snapshot.get("@name", "")) for snapshot in snapshots))
    rows: list[tuple[str, ...]] = []
    for block_key in block_keys:
        model = _lookup(catalog, dsp0[block_key].get("@model"))
        label = model.display_name if model else str(dsp0[block_key].get("@model", block_key))
        states = []
        for snapshot in snapshots:
            state = snapshot.get("blocks", {}).get("dsp0", {}).get(block_key)
            enabled = dsp0[block_key].get("@enabled", True) if state is None else state
            states.append("on" if enabled else "off")
        rows.append((label, *states))

        for name in controllers.get(block_key, {}):
            values = []
            for snapshot in snapshots:
                entry = snapshot.get("controllers", {}).get("dsp0", {}).get(block_key, {}).get(name)
                value = entry.get("@value") if isinstance(entry, dict) else None
                values.append(_format_value(model, name, value))
            rows.append((f"  {name}", *values))
    return render_table(headers, rows)


def _lookup(catalog: ModelCatalog, model_name: Any) -> ModelDefinition | None:
    if not isinstance(model_name, str) or not catalog.has_model(model_name):
        return None
    return catalog.get(model_name)


def _format_value(model: ModelDefinition | None, name: str, value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:.2f}"
    definition = model.parameters.get(name) if model else None
    if definition is not None and definition.forward_map:
        return str(definition.forward_map.get(str(value), value))
    return str(value)

