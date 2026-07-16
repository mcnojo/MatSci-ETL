"""Normalize vision-OCR outputs into a structured JSON envelope.

Two output shapes accepted (chandra emits either based on content; lab models
are prompted for the same shapes — see shared/prompts/vision):
  - layout_html:     <div data-bbox=... data-label=...>...</div> blocks with
                     optional <math>, <chem>, <p>, <b>, tables, etc.
  - figure_analysis: <analyze>[{titles,x_labels,y_labels,x_ticks,y_ticks,legends,series}, ...]</analyze>
                     (chandra may trail a coordinate blob — always junk; ignored.)

`parse(content)` → dict suitable for VisualElement.ocr_parsed, or None if empty.
"""


import json
import re
from html.parser import HTMLParser

_ANALYZE_BLOCK = re.compile(r"<analyze>\s*([\s\S]*?)\s*</analyze>", re.IGNORECASE)
_ANALYZE_OPEN = re.compile(r"<analyze>", re.IGNORECASE)
_MATH_INNER = re.compile(r"<math(?:\s+[^>]*)?>([\s\S]*?)</math>", re.IGNORECASE)
_CHEM_INNER = re.compile(r"<chem(?:\s+[^>]*)?>([\s\S]*?)</chem>", re.IGNORECASE)
_BBOX = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")

# chandra's analyze schema keys -> our singular field names
_ANALYZE_FIELDS = {
    "titles": "title",
    "x_labels": "x_label",
    "y_labels": "y_label",
    "x_ticks": "x_tick",
    "y_ticks": "y_tick",
    "legends": "legend",
    "series": "series",
}


def parse(content: str | None) -> dict | None:
    if not content:
        return None

    # figure_analysis (complete)
    m = _ANALYZE_BLOCK.search(content)
    if m:
        return _build_figure_analysis(m.group(1), truncated=False)

    # figure_analysis (truncated: opener but no closer — salvage what we can)
    if _ANALYZE_OPEN.search(content):
        head = content[content.index("<analyze>") + len("<analyze>"):]
        salvaged = _salvage_json_array(head)
        if salvaged is not None:
            return _build_figure_analysis(salvaged, truncated=True)
        return {"format": "figure_analysis", "truncated": True, "panels": [], "error": "unrecoverable"}

    # layout_html
    if "data-bbox" in content or "data-label" in content:
        return _build_layout_html(content)

    return {"format": "unknown", "raw_head": content[:500]}


# figure_analysis

def _build_figure_analysis(json_str: str, truncated: bool) -> dict:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return {"format": "figure_analysis", "truncated": truncated, "panels": [], "error": f"json: {e}"}

    items = data if isinstance(data, list) else [data]
    panels = []
    for sub in items:
        if not isinstance(sub, dict):
            continue
        panel = {}
        for src_key, dst_key in _ANALYZE_FIELDS.items():
            v = sub.get(src_key)
            if v not in (None, "", []):
                panel[dst_key] = v
        if panel:
            panels.append(panel)

    out = {"format": "figure_analysis", "panels": panels}
    if truncated:
        out["truncated"] = True
    return out


def _salvage_json_array(head: str) -> str | None:
    """Truncated analyze body: trim to last '}' and close the array if it started as one."""
    last_brace = head.rfind("}")
    if last_brace == -1:
        return None
    candidate = head[: last_brace + 1]
    if head.lstrip().startswith("["):
        candidate += "]"
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return None


# layout_html

def _build_layout_html(content: str) -> dict:
    extractor = _BlockExtractor()
    extractor.feed(content)
    extractor.close()
    return {"format": "layout_html", "blocks": extractor.blocks}


class _BlockExtractor(HTMLParser):
    """Pull each top-level <div data-bbox=... data-label=...> into a flat block dict.

    Nested div depth is tracked so an inner <div> inside a labeled block doesn't
    end the outer block prematurely.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._depth = 0       # nesting depth inside the currently-open block
        self._meta: dict | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        is_block = tag == "div" and ("data-bbox" in attrs_d or "data-label" in attrs_d)
        if is_block and self._depth == 0:
            self._meta = {
                "label": attrs_d.get("data-label"),
                "bbox": _parse_bbox(attrs_d.get("data-bbox", "")),
            }
            self._depth = 1
            self._buf = []
            return
        if self._depth > 0:
            if tag == "div":
                self._depth += 1
            self._buf.append(_render_tag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if self._depth > 0:
            self._buf.append(_render_tag(tag, attrs, self_closing=True))

    def handle_endtag(self, tag):
        if self._depth == 0:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._flush()
                return
        self._buf.append(f"</{tag}>")

    def handle_data(self, data):
        if self._depth > 0:
            self._buf.append(data)

    def _flush(self):
        if self._meta is None:
            return
        html = "".join(self._buf).strip()
        self._meta["html"] = html
        self._meta["text"] = _strip_tags(html)
        math = [_strip_tags(m) for m in _MATH_INNER.findall(html)]
        chem = [_strip_tags(c) for c in _CHEM_INNER.findall(html)]
        if math:
            self._meta["math"] = math
        if chem:
            self._meta["chem"] = chem
        self.blocks.append(self._meta)
        self._meta = None
        self._buf = []


def _parse_bbox(s: str) -> list[int] | None:
    m = _BBOX.match(s)
    return [int(m.group(i)) for i in range(1, 5)] if m else None


def _render_tag(tag: str, attrs: list[tuple[str, str | None]], self_closing: bool = False) -> str:
    attr_str = "".join(f' {k}="{v}"' for k, v in attrs if v is not None)
    return f"<{tag}{attr_str}{'/' if self_closing else ''}>"


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
