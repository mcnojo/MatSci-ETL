"""Unit tests for pipeline.table_markdown — chandra HTML tables -> markdown."""

from pipeline.table_markdown import html_table_to_markdown


def test_simple_thead_tbody_table():
    html = """
    <table>
      <thead><tr><th>A</th><th>B</th></tr></thead>
      <tbody>
        <tr><td>1</td><td>2</td></tr>
        <tr><td>3</td><td>4</td></tr>
      </tbody>
    </table>
    """
    md = html_table_to_markdown(html)
    lines = md.splitlines()
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 1 | 2 |"
    assert lines[3] == "| 3 | 4 |"


def test_no_thead_first_row_becomes_header():
    html = "<table><tr><td>H1</td><td>H2</td></tr><tr><td>a</td><td>b</td></tr></table>"
    md = html_table_to_markdown(html)
    lines = md.splitlines()
    assert lines[0] == "| H1 | H2 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| a | b |"


def test_th_row_wins_header_placement():
    html = """
    <table>
      <tr><td>skip</td><td>skip</td></tr>
      <tr><th>Col1</th><th>Col2</th></tr>
      <tr><td>x</td><td>y</td></tr>
    </table>
    """
    md = html_table_to_markdown(html)
    # Header line is the <th> row; the earlier <td>-only row appears as body.
    assert md.splitlines()[0] == "| Col1 | Col2 |"
    assert "skip" in md
    assert "x" in md


def test_ragged_rows_padded_to_max_width():
    html = "<table><tr><th>A</th><th>B</th><th>C</th></tr><tr><td>1</td><td>2</td></tr></table>"
    md = html_table_to_markdown(html)
    lines = md.splitlines()
    # 3-column header, body row padded with an empty cell.
    assert lines[0].count("|") == 4     # opening + 3 separators + closing
    assert lines[2].endswith("|  |")    # trailing empty cell


def test_whitespace_collapse_within_cell():
    html = "<table><tr><td>  hello \n\t world  </td></tr></table>"
    md = html_table_to_markdown(html)
    assert "hello world" in md
    assert "hello  " not in md


def test_pipe_escaped_in_cell_text():
    html = "<table><tr><td>a|b</td><td>c</td></tr></table>"
    md = html_table_to_markdown(html)
    assert "a\\|b" in md
    # Column count is unambiguous on the separator row.
    assert md.splitlines()[1] == "| --- | --- |"


def test_br_inside_cell_becomes_space():
    html = "<table><tr><td>line1<br/>line2</td></tr></table>"
    md = html_table_to_markdown(html)
    assert "line1 line2" in md


def test_empty_table_returns_empty_string():
    assert html_table_to_markdown("<table></table>") == ""
    assert html_table_to_markdown("") == ""


def test_nested_markup_content_preserved():
    html = "<table><tr><td><b>bold</b> value</td></tr></table>"
    md = html_table_to_markdown(html)
    # Inner tags dropped by the parser; text content remains.
    assert "bold value" in md


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    return len(tests)


if __name__ == "__main__":
    n = _run_all()
    print(f"PASS: {n} table_markdown tests")
