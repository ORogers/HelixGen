from collections.abc import Sequence
from typing import Any

from .dataset import ModelCatalog, ModelCatalogError


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
    dsp0 = (
        preset.get("data", {})
        .get("tone", {})
        .get("dsp0", {})
    )
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

    return render_table(("Pos", "Model (ID)", "Type", "Based On"), rows)
