from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .dataset import ModelCatalog, ModelCatalogError


def inspect_preset(preset: Dict[str, Any], catalog: ModelCatalog) -> str:
    dsp0 = (
        preset.get("data", {})
        .get("tone", {})
        .get("dsp0", {})
    )
    rows: List[Tuple[str, str, str, str]] = []

    for key, block in dsp0.items():
        if key in {"inputA", "outputA"}:
            continue
        if not isinstance(block, dict):
            continue
        position = block.get("@position")
        if position is None:
            position_str = "?"
        else:
            position_str = str(position)
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

    headers = ("Pos", "Model (ID)", "Type", "Based On")
    table_rows = [headers] + rows

    widths = [0, 0, 0, 0]
    for row in table_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def format_row(row: Tuple[str, str, str, str]) -> str:
        cells = []
        for idx, cell in enumerate(row):
            cells.append(" " + cell.ljust(widths[idx]) + " ")
        return "│" + "│".join(cells) + "│"

    def make_border(left: str, middle: str, right: str) -> str:
        segments = []
        for width in widths:
            segments.append("─" * (width + 2))
        return left + middle.join(segments) + right

    header_line = format_row(headers)
    separator = make_border("├", "┼", "┤")
    body_lines = [format_row(row) for row in rows]

    table_lines = [
        make_border("┌", "┬", "┐"),
        header_line,
        separator,
    ]
    for line in body_lines:
        table_lines.append(line)
    table_lines.append(make_border("└", "┴", "┘"))
    return "\n".join(table_lines)
