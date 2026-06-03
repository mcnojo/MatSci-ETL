"""ETL tree-building prompts, v1.

DO NOT EDIT casually. These prompts were ported (with small, deliberate
modifications) from the official VectifyAI/PageIndex implementation
(https://github.com/VectifyAI/PageIndex — pageindex/page_index.py,
pageindex/utils.py). The tree-builder pipeline is tuned around their exact
wording; arbitrary rephrasing tends to degrade extraction quality.

If a prompt genuinely needs to change, ship a new version file (`v2.py`)
alongside this one rather than mutating these strings in place.

Each prompt has two variants:
- "upstream": verbatim from VectifyAI/PageIndex. Tuned for large/cloud models
  (GPT-4o, Claude Sonnet/Opus, Gemini Pro).
- "local":   elaborated for small open models (Gemma 4 e4b, Qwen 2.5 7B, etc.).
  Adds explicit negative examples, stricter JSON envelopes, and structural
  guidance the small models need to stay on task.

When upstream and local are identical, both keys point to the same function —
keep them separate so future divergence stays explicit.

Usage:
    from shared.prompts.etl import get_prompt
    prompt = get_prompt("toc_detector_single_page", style, content=text)
"""


import json
from typing import Callable


# toc_detector_single_page

def _toc_detector_single_page_upstream(*, content: str) -> str:
    return f"""
    Your job is to detect if there is a table of content provided in the given text.

    Given text: {content}

    return the following JSON format:
    {{
        "thinking": <why do you think there is a table of content in the given text>
        "toc_detected": "<yes or no>",
    }}

    Directly return the final JSON structure. Do not output anything else.
    Please note: abstract,summary, notation list, figure list, table list, etc. are not table of contents."""


def _toc_detector_single_page_local(*, content: str) -> str:
    return f"""Does this page contain a TABLE OF CONTENTS — a structured listing of
section/chapter titles that serves as a navigation guide for the document?

A real table of contents:
- Lists multiple section or chapter titles in hierarchical or sequential order
- May include page numbers, but not always
- Is explicitly labeled "Contents", "Table of Contents", or similar
- Exists as a dedicated listing, not inline within running text

These are NOT tables of contents — answer "no" for all of these:
- Abstracts or summaries
- Lists of figures, tables, abbreviations, or notation
- Numbered section headings within the body text of a paper
- Reference lists or bibliographies
- Keyword lists
- Author affiliations or acknowledgments
- Journal article headers that list sections inline (e.g. "Introduction ... Methods ... Results")

Most scientific journal articles (typically 4-30 pages) do NOT have a table of contents.
Only answer "yes" if you see a clearly dedicated, structured listing of the document's sections.

Text: {content}

Reply with only this JSON, nothing else:
{{"toc_detected": "yes or no"}}"""


# detect_page_index

def _detect_page_index_upstream(*, toc_content: str) -> str:
    return f"""
    You will be given a table of contents.

    Your job is to detect if there are page numbers/indices given within the table of contents.

    Given text: {toc_content}

    Reply format:
    {{
        "thinking": <why do you think there are page numbers/indices given within the table of contents>
        "page_index_given_in_toc": "<yes or no>"
    }}
    Directly return the final JSON structure. Do not output anything else."""


def _detect_page_index_local(*, toc_content: str) -> str:
    return f"""Does this table of contents contain page numbers or page indices?

Text: {toc_content}

Reply with only this JSON, nothing else:
{{"page_index_given_in_toc": "yes or no"}}"""


# check_if_toc_transformation_is_complete

def _check_if_toc_transformation_is_complete_upstream(*, content: str, toc: str) -> str:
    return (
        """
    You are given a raw table of contents and a  table of contents.
    Your job is to check if the  table of contents is complete.

    Reply format:
    {
        "thinking": <why do you think the cleaned table of contents is complete or not>
        "completed": "yes" or "no"
    }
    Directly return the final JSON structure. Do not output anything else."""
        + "\n Raw Table of contents:\n" + content
        + "\n Cleaned Table of contents:\n" + toc
    )


def _check_if_toc_transformation_is_complete_local(*, content: str, toc: str) -> str:
    return f"""Does the cleaned table of contents contain all sections from the raw table of contents?

Raw Table of contents:
{content}

Cleaned Table of contents:
{toc}

Reply with only this JSON, nothing else:
{{"completed": "yes or no"}}"""


# toc_index_extractor

def _toc_index_extractor_upstream(*, toc, content: str) -> str:
    return (
        """
    You are given a table of contents in a json format and several pages of a document, your job is to add the physical_index to the table of contents in the json format.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    The response should be in the following JSON format:
    [
        {
            "structure": <structure index, "x.x.x" or None> (string),
            "title": <title of the section>,
            "physical_index": "<physical_index_X>" (keep the format)
        },
        ...
    ]

    Only add the physical_index to the sections that are in the provided pages.
    If the section is not in the provided pages, do not add the physical_index to it.
    Directly return the final JSON structure. Do not output anything else."""
        + "\nTable of contents:\n" + str(toc)
        + "\nDocument pages:\n" + content
    )


# local trimmed the per-example "For example..." line; otherwise identical
def _toc_index_extractor_local(*, toc, content: str) -> str:
    return (
        """
    You are given a table of contents in a json format and several pages of a document, your job is to add the physical_index to the table of contents in the json format.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents.

    The response should be in the following JSON format:
    [
        {
            "structure": <structure index, "x.x.x" or None> (string),
            "title": <title of the section>,
            "physical_index": "<physical_index_X>" (keep the format)
        },
        ...
    ]

    Only add the physical_index to the sections that are in the provided pages.
    If the section is not in the provided pages, do not add the physical_index to it.
    Directly return the final JSON structure. Do not output anything else."""
        + "\nTable of contents:\n" + str(toc)
        + "\nDocument pages:\n" + content
    )


# toc_transformer (+ continuation)

def _toc_transformer_upstream(*, toc_content: str) -> str:
    return (
        """
    You are given a table of contents, You job is to transform the whole table of content into a JSON format included table_of_contents.

    structure is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    The response should be in the following JSON format:
    {
    table_of_contents: [
        {
            "structure": <structure index, "x.x.x" or None> (string),
            "title": <title of the section>,
            "page": <page number or None>,
        },
        ...
        ],
    }
    You should transform the full table of contents in one go.
    Directly return the final JSON structure, do not output anything else. """
        + "\n Given table of contents\n:" + toc_content
    )


# Identical to upstream
_toc_transformer_local = _toc_transformer_upstream


def _toc_transformer_continue_upstream(*, toc_content: str, last_complete: str) -> str:
    return f"""
        Your task is to continue the table of contents json structure, directly output the remaining part of the json structure.
        The response should be in the following JSON format:

        The raw table of contents json structure is:
        {toc_content}

        The incomplete transformed table of contents json structure is:
        {last_complete}

        Please continue the json structure, directly output the remaining part of the json structure."""


def _toc_transformer_continue_local(*, toc_content: str, last_complete: str) -> str:
    return f"""
        Your task is to continue the table of contents json structure, directly output the remaining part of the json structure.

        The raw table of contents json structure is:
        {toc_content}

        The incomplete transformed table of contents json structure is:
        {last_complete}

        Please continue the json structure, directly output the remaining part of the json structure."""


# add_page_number_to_toc

def _add_page_number_to_toc_upstream(*, part: str, structure) -> str:
    return (
        """
    You are given an JSON structure of a document and a partial part of the document. Your task is to check if the title that is described in the structure is started in the partial given document.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    If the full target section starts in the partial given document, insert the given JSON structure with the "start": "yes", and "start_index": "<physical_index_X>".

    If the full target section does not start in the partial given document, insert "start": "no",  "start_index": None.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x" or None> (string),
                "title": <title of the section>,
                "start": "<yes or no>",
                "physical_index": "<physical_index_X> (keep the format)" or None
            },
            ...
        ]
    The given structure contains the result of the previous part, you need to fill the result of the current part, do not change the previous result.
    Directly return the final JSON structure. Do not output anything else."""
        + f"\n\nCurrent Partial Document:\n{part}\n\nGiven Structure\n{json.dumps(structure, indent=2)}\n"
    )


_add_page_number_to_toc_local = _add_page_number_to_toc_upstream


# generate_toc_init

def _generate_toc_init_upstream(*, part: str) -> str:
    return (
        """
    You are an expert in extracting hierarchical tree structure, your task is to generate the tree structure of the document.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    For the title, you need to extract the original title from the text, only fix the space inconsistency.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the start and end of page X.

    For the physical_index, you need to extract the physical index of the start of the section from the text. Keep the <physical_index_X> format.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x"> (string),
                "title": <title of the section, keep the original title>,
                "physical_index": "<physical_index_X> (keep the format)"
            },

        ],


    Directly return the final JSON structure. Do not output anything else."""
        + "\nGiven text\n:" + part
    )


_generate_toc_init_local = _generate_toc_init_upstream


# generate_toc_init_with_hint
# Variant used when check_toc found a TOC. The TOC text is included as a
# non-authoritative hint — the model should still verify against the page
# content because TOCs sometimes omit sections or use different titles.

def _generate_toc_init_with_hint_upstream(*, part: str, toc_hint: str) -> str:
    return (
        """
    You are an expert in extracting hierarchical tree structure, your task is to generate the tree structure of the document.

    A table of contents has been extracted from this document and is provided below as a hint. Use it to guide your structure extraction, but verify each section against the actual page content — TOCs sometimes omit sections or use abbreviated titles. Prefer the title as it appears in the page body.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    For the title, you need to extract the original title from the text, only fix the space inconsistency.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the start and end of page X.

    For the physical_index, you need to extract the physical index of the start of the section from the text. Keep the <physical_index_X> format.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x"> (string),
                "title": <title of the section, keep the original title>,
                "physical_index": "<physical_index_X> (keep the format)"
            },

        ],


    Directly return the final JSON structure. Do not output anything else."""
        + "\nTable of contents (hint):\n" + toc_hint
        + "\nGiven text\n:" + part
    )


_generate_toc_init_with_hint_local = _generate_toc_init_with_hint_upstream


# generate_toc_continue

def _generate_toc_continue_upstream(*, toc_content, part: str) -> str:
    return (
        """
    You are an expert in extracting hierarchical tree structure.
    You are given a tree structure of the previous part and the text of the current part.
    Your task is to continue the tree structure from the previous part to include the current part.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    For the title, you need to extract the original title from the text, only fix the space inconsistency.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the start and end of page X. \

    For the physical_index, you need to extract the physical index of the start of the section from the text. Keep the <physical_index_X> format.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x"> (string),
                "title": <title of the section, keep the original title>,
                "physical_index": "<physical_index_X> (keep the format)"
            },
            ...
        ]

    Directly return the additional part of the final JSON structure. Do not output anything else."""
        + "\nGiven text\n:" + part
        + "\nPrevious tree structure\n:" + json.dumps(toc_content, indent=2)
    )


_generate_toc_continue_local = _generate_toc_continue_upstream


# check_title_appearance

def _check_title_appearance_upstream(*, title: str, page_text: str) -> str:
    return f"""
    Your job is to check if the given section appears or starts in the given page_text.

    Note: do fuzzy matching, ignore any space inconsistency in the page_text.

    The given section title is {title}.
    The given page_text is {page_text}.

    Reply format:
    {{

        "thinking": <why do you think the section appears or starts in the page_text>
        "answer": "yes or no" (yes if the section appears or starts in the page_text, no otherwise)
    }}
    Directly return the final JSON structure. Do not output anything else."""


def _check_title_appearance_local(*, title: str, page_text: str) -> str:
    return f"""Does the section titled "{title}" appear or start in this page text? Use fuzzy matching, ignore spacing differences.

Page text: {page_text}

Reply with only this JSON, nothing else:
{{"answer": "yes or no"}}"""


# check_title_appearance_in_start

def _check_title_appearance_in_start_upstream(*, title: str, page_text: str) -> str:
    return f"""
    You will be given the current section title and the current page_text.
    Your job is to check if the current section starts in the beginning of the given page_text.
    If there are other contents before the current section title, then the current section does not start in the beginning of the given page_text.
    If the current section title is the first content in the given page_text, then the current section starts in the beginning of the given page_text.

    Note: do fuzzy matching, ignore any space inconsistency in the page_text.

    The given section title is {title}.
    The given page_text is {page_text}.

    reply format:
    {{
        "thinking": <why do you think the section appears or starts in the page_text>
        "start_begin": "yes or no" (yes if the section starts in the beginning of the page_text, no otherwise)
    }}
    Directly return the final JSON structure. Do not output anything else."""


def _check_title_appearance_in_start_local(*, title: str, page_text: str) -> str:
    return f"""Is the section titled "{title}" the very first content on this page? Answer "no" if other content appears before it.

Page text: {page_text}

Reply with only this JSON, nothing else:
{{"start_begin": "yes or no"}}"""


# single_toc_item_index_fixer

def _single_toc_item_index_fixer_upstream(*, section_title, content: str) -> str:
    return (
        """
    You are given a section title and several pages of a document, your job is to find the physical index of the start page of the section in the partial document.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    Reply in a JSON format:
    {
        "thinking": <explain which page, started and closed by <physical_index_X>, contains the start of this section>,
        "physical_index": "<physical_index_X>" (keep the format)
    }
    Directly return the final JSON structure. Do not output anything else."""
        + "\nSection Title:\n" + str(section_title)
        + "\nDocument pages:\n" + content
    )


# local dropped the inline <physical_index_X> hint inside "thinking"
def _single_toc_item_index_fixer_local(*, section_title, content: str) -> str:
    return (
        """
    You are given a section title and several pages of a document, your job is to find the physical index of the start page of the section in the partial document.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    Reply in a JSON format:
    {
        "thinking": <explain which page contains the start of this section>,
        "physical_index": "<physical_index_X>" (keep the format)
    }
    Directly return the final JSON structure. Do not output anything else."""
        + "\nSection Title:\n" + str(section_title)
        + "\nDocument pages:\n" + content
    )


# generate_node_summary
# node_text is wrapped with context markers:
#   <<<context-before>>>{...}<<<section-content>>>{...}<<<context-after>>>{...}
# The summary covers ONLY <<<section-content>>>. Context blocks exist solely to
# disambiguate sentences cut at page boundaries.

def _generate_node_summary_upstream(*, node_text: str) -> str:
    return f"""You are given a section of a document with surrounding context. Generate a description of the main points covered in the section.

    The text is wrapped with markers:
      <<<context-before>>>  — pages immediately before this section (for boundary disambiguation only; do NOT summarize)
      <<<section-content>>> — the section itself (summarize ONLY this)
      <<<context-after>>>   — pages immediately after this section (for boundary disambiguation only; do NOT summarize)

    Document Text:
    {node_text}

    Directly return the description of the <<<section-content>>>, do not include any other text.
    """


_generate_node_summary_local = _generate_node_summary_upstream


# verify_node_summary
# Fast-model fidelity check: does the summary cover the section's content?

def _verify_node_summary_upstream(*, title: str, section_text: str, summary: str) -> str:
    return f"""You are checking whether a section summary faithfully covers its source content.

Section title: {title}

Section content:
{section_text}

Proposed summary:
{summary}

Reply with only this JSON, nothing else:
{{"faithful": "yes or no", "missed_topics": ["topic 1", "topic 2", ...]}}

"faithful" is "yes" only if the summary captures every major topic in the section. "missed_topics" lists any major topics present in the section content but absent from the summary. If "faithful" is "yes", "missed_topics" must be []."""


_verify_node_summary_local = _verify_node_summary_upstream


# regenerate_summary_with_missed_topics

def _regenerate_summary_with_missed_topics_upstream(
    *, node_text: str, prior_summary: str, missed_topics: list,
) -> str:
    missed = "\n".join(f"  - {t}" for t in missed_topics) if missed_topics else "  - (none)"
    return f"""Re-write the section summary below to include the topics it currently misses.

Section text (same context markers as before — summarize only <<<section-content>>>):
{node_text}

Prior summary:
{prior_summary}

Topics the prior summary missed:
{missed}

Return only the revised summary text, no preamble."""


_regenerate_summary_with_missed_topics_local = _regenerate_summary_with_missed_topics_upstream


# figure_aware_resummarize
# Used in Stage E to re-write a leaf node's summary using per-figure data.

def _figure_aware_resummarize_upstream(
    *, title: str, prior_summary: str, figure_block: str,
) -> str:
    return f"""Revise the summary of a document section to integrate information from its figures, tables, and equations.

Section title: {title}

Prior summary (text-only):
{prior_summary}

Visual elements detected in this section (each block is one element):
{figure_block}

Write a revised summary that:
  - Preserves the factual content of the prior summary
  - Integrates findings, data points, and labeled species from the visual elements
  - Cites figures/tables/equations by element_id when relevant (e.g. "Figure fig_doc_0007_001 shows...")
  - Stays roughly the same length as the prior summary

Return only the revised summary text, no preamble."""


_figure_aware_resummarize_local = _figure_aware_resummarize_upstream


# summarize_from_children
# Bottom-up parent summary derived from child summaries (no figure data, no OCR).

def _summarize_from_children_upstream(
    *, title: str, child_summaries: str,
) -> str:
    return f"""You are summarizing a parent section from the summaries of its child subsections.

Parent section title: {title}

Child subsection summaries (in document order):
{child_summaries}

Write a single coherent summary of the parent section that synthesizes the children. Roughly the length of one child summary. Return only the summary, no preamble."""


_summarize_from_children_local = _summarize_from_children_upstream


# generate_doc_description

def _generate_doc_description_upstream(*, structure) -> str:
    return f"""Your are an expert in generating descriptions for a document.
    You are given a structure of a document. Your task is to generate a one-sentence description for the document, which makes it easy to distinguish the document from other documents.

    Document Structure: {structure}

    Directly return the description, do not include any other text.
    """


def _generate_doc_description_local(*, structure) -> str:
    return f"""Your are an expert in generating descriptions for a document.
    You are given a structure of a document. Your task is to generate a one-sentence description for the document, which makes it easy to distinguish the document from other documents.

    Document Structure: {structure}

    Directly return the description, do not include any other text."""


# Registry & dispatcher

_PROMPTS: dict[str, dict[str, Callable[..., str]]] = {
    "toc_detector_single_page": {
        "upstream": _toc_detector_single_page_upstream,
        "local": _toc_detector_single_page_local,
    },
    "detect_page_index": {
        "upstream": _detect_page_index_upstream,
        "local": _detect_page_index_local,
    },
    "check_if_toc_transformation_is_complete": {
        "upstream": _check_if_toc_transformation_is_complete_upstream,
        "local": _check_if_toc_transformation_is_complete_local,
    },
    "toc_index_extractor": {
        "upstream": _toc_index_extractor_upstream,
        "local": _toc_index_extractor_local,
    },
    "toc_transformer": {
        "upstream": _toc_transformer_upstream,
        "local": _toc_transformer_local,
    },
    "toc_transformer_continue": {
        "upstream": _toc_transformer_continue_upstream,
        "local": _toc_transformer_continue_local,
    },
    "add_page_number_to_toc": {
        "upstream": _add_page_number_to_toc_upstream,
        "local": _add_page_number_to_toc_local,
    },
    "generate_toc_init": {
        "upstream": _generate_toc_init_upstream,
        "local": _generate_toc_init_local,
    },
    "generate_toc_init_with_hint": {
        "upstream": _generate_toc_init_with_hint_upstream,
        "local": _generate_toc_init_with_hint_local,
    },
    "generate_toc_continue": {
        "upstream": _generate_toc_continue_upstream,
        "local": _generate_toc_continue_local,
    },
    "check_title_appearance": {
        "upstream": _check_title_appearance_upstream,
        "local": _check_title_appearance_local,
    },
    "check_title_appearance_in_start": {
        "upstream": _check_title_appearance_in_start_upstream,
        "local": _check_title_appearance_in_start_local,
    },
    "single_toc_item_index_fixer": {
        "upstream": _single_toc_item_index_fixer_upstream,
        "local": _single_toc_item_index_fixer_local,
    },
    "generate_node_summary": {
        "upstream": _generate_node_summary_upstream,
        "local": _generate_node_summary_local,
    },
    "verify_node_summary": {
        "upstream": _verify_node_summary_upstream,
        "local": _verify_node_summary_local,
    },
    "regenerate_summary_with_missed_topics": {
        "upstream": _regenerate_summary_with_missed_topics_upstream,
        "local": _regenerate_summary_with_missed_topics_local,
    },
    "figure_aware_resummarize": {
        "upstream": _figure_aware_resummarize_upstream,
        "local": _figure_aware_resummarize_local,
    },
    "summarize_from_children": {
        "upstream": _summarize_from_children_upstream,
        "local": _summarize_from_children_local,
    },
    "generate_doc_description": {
        "upstream": _generate_doc_description_upstream,
        "local": _generate_doc_description_local,
    },
}


VALID_STYLES = ("upstream", "local")


def get_prompt(name: str, style: str, **kwargs) -> str:
    """Render a prompt by name and style.

    Falls back to the other style if the requested one isn't registered.
    Raises KeyError for unknown names or ValueError for unknown styles.
    """
    if style not in VALID_STYLES:
        raise ValueError(
            f"Unknown prompt style {style!r}; expected one of {VALID_STYLES}"
        )
    if name not in _PROMPTS:
        raise KeyError(f"Unknown prompt: {name!r}")

    variants = _PROMPTS[name]
    fn = variants.get(style)
    if fn is None:
        fallback_style = "local" if style == "upstream" else "upstream"
        fn = variants.get(fallback_style)
    if fn is None:
        raise RuntimeError(f"No variants registered for prompt {name!r}")
    return fn(**kwargs)
