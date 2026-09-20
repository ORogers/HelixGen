from hlxgen.inspector import render_table


def test_render_table_borders_line_up_with_the_rows_they_frame() -> None:
    """Every line of a table is the same width, and the column rules align.

    The `models` table used to draw its borders with a one-character join while
    padding its cells with a three-character one, so the top and bottom rules
    sat two characters short per column and the '┬' marks drifted left of the
    '│' below them. Character counts alone would not have caught the drift.
    """
    table = render_table(
        ("Model", "Internal ID", "Based On"),
        [
            ("63 Spring Reverb", "HD2_Reverb63Spring", ""),
            ("Plate", "HD2_ReverbPlate", "Line 6 Original"),
        ],
    ).splitlines()

    assert len({len(line) for line in table}) == 1, "rows are not all the same width"

    def rule_columns(line: str, marks: str) -> list[int]:
        return [idx for idx, char in enumerate(line) if char in marks]

    expected = rule_columns(table[1], "│")
    for line in table:
        marks = "│" if line.startswith("│") else "┌┬┐├┼┤└┴┘"
        assert rule_columns(line, marks) == expected, f"column rules drift on {line!r}"


def test_render_table_sizes_columns_to_the_widest_cell() -> None:
    table = render_table(("A", "B"), [("wide cell", "x")]).splitlines()

    assert table[1] == "│ A         │ B │"
    assert table[3] == "│ wide cell │ x │"
