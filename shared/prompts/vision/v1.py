"""Vision OCR prompts, v1.

`CHANDRA_*` — datalab-to/chandra-ocr-2 hosted on vLLM. Trained for the
data-bbox / data-label / <analyze> output shape below.
`LAB_ELEMENT_OCR_PROMPT` — GPT-4o / Claude vision on element crops. Elicits
the same parseable output shape so `pipeline.vision_parser` handles either.
"""

CHANDRA_ALLOWED_TAGS = [
    "math", "br", "i", "b", "u", "del", "sup", "sub", "table", "tr", "td",
    "p", "th", "div", "pre", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "li",
    "input", "a", "span", "img", "hr", "tbody", "small", "caption", "strong",
    "thead", "big", "code", "chem",
]
CHANDRA_ALLOWED_ATTRS = [
    "class", "colspan", "rowspan", "display", "checked", "type", "border",
    "value", "style", "href", "alt", "align", "data-bbox", "data-label",
]
CHANDRA_PROMPT_ENDING = f"""
Only use these tags {CHANDRA_ALLOWED_TAGS}, and these attributes {CHANDRA_ALLOWED_ATTRS}.

Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural.
""".strip()

CHANDRA_OCR_LAYOUT_PROMPT = f"""
OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.

Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page

{CHANDRA_PROMPT_ENDING}
""".strip()

CHANDRA_OCR_PROMPT = f"""
OCR this image to HTML.

{CHANDRA_PROMPT_ENDING}
""".strip()


# Element-crop OCR for GPT-4o / Claude vision. Input is a single figure /
# table / formula crop, not a full page. Output must match one of the two
# shapes `pipeline.vision_parser` recognizes:
#   1. <analyze>[{...}]</analyze>  → chart/plot with structured axes/series.
#   2. <div data-bbox="0 0 1000 1000" data-label="...">...</div>  → everything
#      else (tables, text, formulas, diagrams, chemistry).
# data-bbox is fixed to "0 0 1000 1000" because the crop IS the element —
# there's no meaningful sub-region to point at.

_LAB_MODE_A = """MODE A — chart / plot / graph figures.
If the image is a plotted chart with axes (bar, line, scatter, box, etc.),
emit ONE <analyze>...</analyze> block wrapping a JSON array of one panel
object per chart panel visible. Each panel object uses these keys (omit any
that don't apply; keep arrays in visual order):
  "titles":   [string]  chart / panel titles
  "x_labels": [string]  x-axis labels
  "y_labels": [string]  y-axis labels
  "x_ticks":  [string]  x-axis tick values (in order)
  "y_ticks":  [string]  y-axis tick values (in order)
  "legends":  [string]  legend entries
  "series":   [{"name": string, "points": [[x, y], ...]}]

Example:
<analyze>[{"titles":["Cycle Performance"],"x_labels":["Cycle number"],"y_labels":["Capacity (mAh/g)"],"legends":["G2","R-G2"],"series":[{"name":"G2","points":[[1,120],[2,115]]},{"name":"R-G2","points":[[1,145],[2,144]]}]}]</analyze>
""".strip()

_LAB_MODE_B = """MODE B — everything else.
Emit one or more <div data-bbox="0 0 1000 1000" data-label="LABEL">...</div>
blocks. LABEL is exactly one of:
  Figure, Table, Section-Header, Text, Caption, Equation-Block, Diagram,
  Chemical-Block, Code-Block, List-Group, Bibliography.

Inside each block:
  - Text paragraphs use <p>...</p>; <br> only when a line break carries meaning.
  - Tables use <table>/<thead>/<tbody>/<tr>/<th>/<td> with colspan/rowspan
    matching the source structure.
  - Math uses <math>...</math> in KaTeX-compatible LaTeX (inline and block).
  - Chemistry uses <chem>...</chem> (SMILES preferred where identifiable).
  - Sub/superscripts use <sub>/<sup>; preserve spacing and special characters.
  - Section headers: <h1>...</h5> inside a Section-Header block.
""".strip()

LAB_ELEMENT_OCR_PROMPT = f"""
You are OCR'ing a single element crop from a scientific paper — one figure,
table, formula, or text block. Return your output in exactly ONE of the two
modes below. Do not mix modes. Do not wrap output in code fences. Return
ONLY the tagged output — no preamble, no trailing commentary.

{_LAB_MODE_A}

{_LAB_MODE_B}

Global rules:
  - Reading order is natural: top-to-bottom, left-to-right.
  - Do NOT invent content not present in the image.
  - Preserve all math, chemistry, subscripts, superscripts, and units verbatim.
  - When the element is a chart AND has readable data points, prefer MODE A.
    When you can't reliably read tick values or series points, fall back to
    MODE B with data-label="Figure" and describe the chart inside.
""".strip()
