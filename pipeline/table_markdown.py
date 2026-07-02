"""Chandra <table>...</table> HTML -> GitHub-flavored markdown.

Chandra's OCR emits `<table>` fragments inside a wider layout_html envelope.
The chunker consumes them as text; embedding raw HTML into the retrieval
index is noisy garbage, so we normalize to markdown at the enrichment stage
where table structure is authoritative.

Header row is the first row containing any <th>, or the first row overall if
none use <th>. Cells collapse internal whitespace and escape `|`. Malformed
HTML yields whatever parses; the rest is dropped rather than raised.
"""

from __future__ import annotations

from html.parser import HTMLParser


def html_table_to_markdown(html: str) -> str:
    """Convert one <table>…</table> fragment to a markdown table. Empty on no rows."""
    parser = _TableToMarkdown()
    parser.feed(html)
    parser.close()
    return parser.to_markdown()


class _TableToMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._row_has_th: bool = False
        self._header_row_index: int | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self._row_has_th = False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row_has_th = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                raw = "".join(self._cell)
                cell = " ".join(raw.split()).replace("|", "\\|")
                self._row.append(cell)
            self._cell = None
        elif tag == "tr":
            if self._row is not None and self._row:
                self._rows.append(self._row)
                if self._row_has_th and self._header_row_index is None:
                    self._header_row_index = len(self._rows) - 1
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def to_markdown(self) -> str:
        if not self._rows:
            return ""
        header_idx = self._header_row_index if self._header_row_index is not None else 0
        header = self._rows[header_idx]
        body = [r for i, r in enumerate(self._rows) if i != header_idx]
        width = max(len(header), max((len(r) for r in body), default=0))
        header = header + [""] * (width - len(header))
        lines = ["| " + " | ".join(header) + " |",
                 "| " + " | ".join(["---"] * width) + " |"]
        for row in body:
            lines.append("| " + " | ".join(row + [""] * (width - len(row))) + " |")
        return "\n".join(lines)
