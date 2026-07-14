"""PageRangeSpec → rendered page text. Wrapping rules live here once."""

import re

from shared.temporal.activity_models import PageRangeSpec


# cl100k overhead per page for `<physical_index_N>\n…\n<physical_index_N>\n\n`.
_PHYSICAL_INDEX_OVERHEAD_TOKENS = 15
_DOTS_RE_LONG = re.compile(r"\.{5,}")
_DOTS_RE_SPACED = re.compile(r"(?:\. ){5,}\.?")


def physical_index_overhead_tokens() -> int:
    return _PHYSICAL_INDEX_OVERHEAD_TOKENS


def transform_dots(text: str) -> str:
    text = _DOTS_RE_LONG.sub(": ", text)
    text = _DOTS_RE_SPACED.sub(": ", text)
    return text


def _wrap_physical_index(text: str, page_index_one_based: int) -> str:
    return (
        f"<physical_index_{page_index_one_based}>\n"
        f"{text}\n"
        f"<physical_index_{page_index_one_based}>\n\n"
    )


def _join_indices(pages: list[tuple[str, int]], indices: list[int], wrap: str) -> str:
    if wrap == "raw":
        return "".join(pages[i][0] for i in indices)
    if wrap == "physical_index":
        return "".join(_wrap_physical_index(pages[i][0], i + 1) for i in indices)
    raise ValueError(f"PageRangeSpec.wrap must be 'raw' or 'physical_index', got {wrap!r}")


def _extract_section_block(text: str) -> str:
    start_marker = "<<<section-content>>>"
    end_marker = "<<<context-after>>>"
    si = text.find(start_marker)
    if si == -1:
        return text
    si += len(start_marker)
    ei = text.find(end_marker, si)
    return text[si:ei].strip() if ei != -1 else text[si:].strip()


def assemble_page_text(pages: list[tuple[str, int]], spec: PageRangeSpec) -> str:
    core = _join_indices(pages, spec.indices, spec.wrap)
    if spec.transform_dots:
        core = transform_dots(core)

    if spec.overlap_pre or spec.overlap_post:
        # Overlap markers wrap raw text; pre-wrapped pages would break section extraction.
        if spec.wrap != "raw":
            raise ValueError(
                f"PageRangeSpec.overlap_* requires wrap='raw'; got wrap={spec.wrap!r}"
            )
        pre = _join_indices(pages, spec.overlap_pre, "raw") if spec.overlap_pre else ""
        post = _join_indices(pages, spec.overlap_post, "raw") if spec.overlap_post else ""
        wrapped = (
            f"<<<context-before>>>\n{pre}\n"
            f"<<<section-content>>>\n{core}\n"
            f"<<<context-after>>>\n{post}"
        )
        return _extract_section_block(wrapped) if spec.extract_section_only else wrapped

    # No overlaps → nothing to strip; core IS the section.
    return core
