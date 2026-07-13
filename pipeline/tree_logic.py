"""Pure document-tree orchestration; sandbox-safe.

LLM I/O via `call_llm`, randomness via `rng`. Page text is referenced by
0-based index through `PromptSpec.page_kwargs` + `opt.pages_uri`; never
inlined into activity inputs.
"""

import asyncio
import copy
import json
import logging
import math
import random
import re
from typing import Protocol

import tiktoken
from pydantic import BaseModel, ConfigDict

from shared.schemas import DocumentTree, TreeNode
from shared.temporal.activity_models import PageRangeSpec, PromptSpec

from .page_assembly import physical_index_overhead_tokens

log = logging.getLogger("tree_logic")


class LlmResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    content: str
    finish_reason: str


class CallLlm(Protocol):
    """Bridge from workflow → LLM activities.

    `__call__` returns raw text — used by the toc_transformer continuation loop,
    where multiple partial responses are concatenated before final parse.
    `parsed` returns a validated Pydantic model dumped to a dict, keyed by
    response_schemas.RESPONSE_SCHEMAS. instructor owns fence-stripping /
    ValidationError repair inside the activity; the workflow just reads the dict.
    """
    async def __call__(
        self, model: str, spec: PromptSpec, *,
        json_mode: bool = False, temperature: float = 0.0,
    ) -> LlmResult: ...

    async def parsed(
        self, model: str, spec: PromptSpec, response_schema: str,
        *, temperature: float = 0.0,
    ) -> dict: ...


class BuildOpt(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    model_fast: str
    prompt_style: str            # "local" | "upstream"
    pages_uri: str
    toc_check_page_num: int
    max_page_num_each_node: int
    max_token_num_each_node: int
    if_add_node_id: str          # "yes" | "no"
    if_add_node_summary: str
    if_add_doc_description: str
    summary_overlap_pages: int
    verify_summaries: bool


def build_opt_from_config(config: dict, pages_uri: str) -> BuildOpt:
    tree_cfg = config["tree"]
    llm_cfg = config["tree_llm"]

    prompt_style = (llm_cfg.get("prompt_style") or "local").lower()
    if prompt_style not in ("local", "upstream"):
        raise ValueError(
            f"tree_llm.prompt_style must be 'local' or 'upstream', got {prompt_style!r}"
        )

    return BuildOpt(
        model=llm_cfg["model"],
        model_fast=llm_cfg.get("model_fast") or llm_cfg["model"],
        prompt_style=prompt_style,
        pages_uri=pages_uri,
        toc_check_page_num=tree_cfg["toc_check_pages"],
        max_page_num_each_node=tree_cfg["max_pages_per_node"],
        max_token_num_each_node=tree_cfg["max_tokens_per_node"],
        if_add_node_id="yes" if tree_cfg.get("add_node_id", True) else "no",
        if_add_node_summary="yes" if tree_cfg.get("add_node_summary", True) else "no",
        if_add_doc_description="yes" if tree_cfg.get("add_doc_description", False) else "no",
        summary_overlap_pages=tree_cfg.get("summary_overlap_pages", 1),
        verify_summaries=tree_cfg.get("verify_summaries", True),
    )


def _spec(name: str, opt: BuildOpt, *, small=None, page=None) -> PromptSpec:
    return PromptSpec(
        name=name, style=opt.prompt_style,
        small_kwargs=small or {}, page_kwargs=page or {},
        pages_uri=opt.pages_uri if page else None,
    )


def _page(indices, *, wrap="raw", transform_dots=False,
          overlap_pre=(), overlap_post=(), extract_section_only=False) -> PageRangeSpec:
    return PageRangeSpec(
        indices=list(indices), wrap=wrap, transform_dots=transform_dots,
        overlap_pre=list(overlap_pre), overlap_post=list(overlap_post),
        extract_section_only=extract_section_only,
    )


_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_enc.encode(text))


def _get_json_content(response: str) -> str:
    # Strip ```json / ``` fences off the toc_transformer's per-round partial.
    # instructor already handles this internally; this helper only survives on
    # the assembly path where we concatenate multiple raw text responses.
    start = response.find("```json")
    if start != -1:
        response = response[start + 7:]
    end = response.rfind("```")
    if end != -1:
        response = response[:end]
    return response.strip()


def _parse_toc_assembly(text: str) -> list[dict]:
    """Parse the toc_transformer's assembled multi-round JSON.

    Prompt style + provider drift produce {"toc":[...]}, {"table_of_contents":[...]},
    {"contents":[...]}, or a bare array. Returns raw dicts — downstream mutates
    via convert_physical_index_to_int / convert_page_to_int in place.
    """
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("toc_transformer: JSON parse failed: %s", exc)
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("toc", "table_of_contents", "contents"):
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        if len(parsed) == 1:
            val = next(iter(parsed.values()))
            if isinstance(val, list):
                return val
    return []


def convert_physical_index_to_int(data):
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "physical_index" in item:
                val = item["physical_index"]
                if isinstance(val, str):
                    if val.startswith("<physical_index_"):
                        item["physical_index"] = int(val.split("_")[-1].rstrip(">").strip())
                    elif val.startswith("physical_index_"):
                        item["physical_index"] = int(val.split("_")[-1].strip())
    elif isinstance(data, str):
        if data.startswith("<physical_index_"):
            return int(data.split("_")[-1].rstrip(">").strip())
        elif data.startswith("physical_index_"):
            return int(data.split("_")[-1].strip())
        return None
    return data


def convert_page_to_int(data):
    for item in data:
        if "page" in item and isinstance(item["page"], str):
            try:
                item["page"] = int(item["page"])
            except ValueError:
                pass
    return data


def write_node_id(data, node_id=0):
    if isinstance(data, dict):
        data["node_id"] = str(node_id).zfill(4)
        node_id += 1
        if "nodes" in data:
            node_id = write_node_id(data["nodes"], node_id)
    elif isinstance(data, list):
        for item in data:
            node_id = write_node_id(item, node_id)
    return node_id


def list_to_tree(data):
    def get_parent_structure(structure):
        if not structure:
            return None
        parts = str(structure).split(".")
        return ".".join(parts[:-1]) if len(parts) > 1 else None

    nodes = {}
    root_nodes = []

    for item in data:
        structure = item.get("structure")
        node = {
            "title": item.get("title"),
            "start_index": item.get("start_index"),
            "end_index": item.get("end_index"),
            "nodes": [],
        }
        nodes[structure] = node
        parent = get_parent_structure(structure)
        if parent and parent in nodes:
            nodes[parent]["nodes"].append(node)
        else:
            root_nodes.append(node)

    def clean_node(node):
        if not node["nodes"]:
            del node["nodes"]
        else:
            for child in node["nodes"]:
                clean_node(child)
        return node

    return [clean_node(n) for n in root_nodes]


def add_preface_if_needed(data):
    if not isinstance(data, list) or not data:
        return data
    if data[0].get("physical_index") is not None and data[0]["physical_index"] > 1:
        data.insert(0, {"structure": "0", "title": "Preface", "physical_index": 1})
    return data


def validate_and_truncate_physical_indices(toc, page_list_length, start_index=1):
    if not toc:
        return toc
    max_allowed = page_list_length + start_index - 1
    for item in toc:
        if item.get("physical_index") is not None:
            if item["physical_index"] > max_allowed:
                log.debug(
                    "Removed physical_index for '%s' (was %d, beyond document)",
                    item.get("title"), item["physical_index"],
                )
                item["physical_index"] = None
    return toc


def page_list_to_group_indices(
    page_indices: list[int],
    token_counts: list[int],
    max_tokens: int = 10000,
    overlap_page: int = 1,
    per_page_overhead_tokens: int = 0,
) -> list[list[int]]:
    # per_page_overhead models marker-wrapping cost when callers wrap with <physical_index_N>.
    effective = [token_counts[i] + per_page_overhead_tokens for i in page_indices]
    num_tokens = sum(effective)
    if num_tokens <= max_tokens:
        return [list(page_indices)]

    expected_parts = math.ceil(num_tokens / max_tokens)
    avg_tokens = math.ceil(((num_tokens / expected_parts) + max_tokens) / 2)

    groups: list[list[int]] = []
    current: list[int] = []
    current_count = 0

    for k, (idx, tokens) in enumerate(zip(page_indices, effective)):
        if current_count + tokens > avg_tokens and current:
            groups.append(current)
            overlap_start = max(k - overlap_page, 0)
            current = list(page_indices[overlap_start:k])
            current_count = sum(effective[overlap_start:k])
        current.append(idx)
        current_count += tokens

    if current:
        groups.append(current)

    return groups


# Citation-shape guardrail against LLM over-decomposition of bibliographies.
# Two orthogonal signals (either is sufficient):
#   INITIALS — leading author initials, e.g. "S. Y. Sayed, K. P. Yao, ..."
#   JOURNAL  — vol/year/page triple, e.g. ", 2016, 6, 1600757."
# When >= MIN_RUN consecutive sibling leaves match, they collapse to one
# "References" node. Runs shorter than that are left alone (avoid clobbering
# a real 2-3-item subsection that happens to look citation-shaped).
_CITATION_INITIALS_RE = re.compile(r"^([A-Z]\.[\s ]*){1,3}[A-Z][A-Za-zÀ-ſ´'’`\-]+")
_CITATION_JOURNAL_RE = re.compile(r",\s*(19|20)\d{2},\s*\d+,\s*\d+")
_CITATION_MIN_RUN = 5


def _looks_like_citation(title) -> bool:
    if not isinstance(title, str) or not title:
        return False
    return bool(_CITATION_INITIALS_RE.match(title) or _CITATION_JOURNAL_RE.search(title))


def _collapse_citation_run(children: list[dict]) -> list[dict]:
    # Scan siblings left-to-right; merge maximal runs of leaves whose titles
    # look like citations. Leaves = no "nodes" key (see list_to_tree's cleanup).
    out: list[dict] = []
    i = 0
    while i < len(children):
        if children[i].get("nodes") or not _looks_like_citation(children[i].get("title", "")):
            out.append(children[i]); i += 1; continue
        j = i
        while (j < len(children)
               and not children[j].get("nodes")
               and _looks_like_citation(children[j].get("title", ""))):
            j += 1
        run = children[i:j]
        if len(run) >= _CITATION_MIN_RUN:
            starts = [n.get("start_index") for n in run if n.get("start_index") is not None]
            ends   = [n.get("end_index")   for n in run if n.get("end_index")   is not None]
            merged = {
                "title": "References",
                "start_index": min(starts) if starts else None,
                "end_index":   max(ends)   if ends   else None,
                "nodes": [],
            }
            log.warning(
                "collapsed %d citation-shaped sibling leaves into 'References' (pp %s-%s)",
                len(run), merged["start_index"], merged["end_index"],
            )
            out.append(merged)
        else:
            out.extend(run)
        i = j
    return out


def collapse_over_decomposed_leaves(tree):
    # Bottom-up so a citation run inside a nested parent is caught before its
    # parent is evaluated. Idempotent: re-running yields the same tree.
    if not isinstance(tree, list):
        return tree
    for node in tree:
        if isinstance(node, dict) and node.get("nodes"):
            node["nodes"] = collapse_over_decomposed_leaves(node["nodes"])
    return _collapse_citation_run(tree)


def post_processing(structure, end_physical_index):
    for i, item in enumerate(structure):
        item["start_index"] = item.get("physical_index")
        if i < len(structure) - 1:
            nxt = structure[i + 1]
            if nxt.get("appear_start") == "yes":
                item["end_index"] = nxt["physical_index"] - 1
            else:
                item["end_index"] = nxt["physical_index"]
        else:
            item["end_index"] = end_physical_index

    tree = list_to_tree(structure)
    if tree:
        return collapse_over_decomposed_leaves(tree)

    for node in structure:
        node.pop("appear_start", None)
        node.pop("physical_index", None)
    return collapse_over_decomposed_leaves(structure)


def structure_to_list(structure):
    if isinstance(structure, dict):
        nodes = [structure]
        if "nodes" in structure:
            nodes.extend(structure_to_list(structure["nodes"]))
        return nodes
    elif isinstance(structure, list):
        nodes = []
        for item in structure:
            nodes.extend(structure_to_list(item))
        return nodes
    return []


def remove_page_number(data):
    if isinstance(data, dict):
        data.pop("page_number", None)
        for key in list(data.keys()):
            if "nodes" in key:
                remove_page_number(data[key])
    elif isinstance(data, list):
        for item in data:
            remove_page_number(item)
    return data


def format_structure(structure, order=None):
    if not order:
        return structure
    if isinstance(structure, dict):
        if "nodes" in structure:
            structure["nodes"] = format_structure(structure["nodes"], order)
        if not structure.get("nodes"):
            structure.pop("nodes", None)
        structure = {k: structure[k] for k in order if k in structure}
    elif isinstance(structure, list):
        structure = [format_structure(item, order) for item in structure]
    return structure


def create_clean_structure_for_description(structure):
    if isinstance(structure, dict):
        clean = {}
        for key in ("title", "node_id", "summary"):
            if key in structure:
                clean[key] = structure[key]
        if "nodes" in structure and structure["nodes"]:
            clean["nodes"] = create_clean_structure_for_description(structure["nodes"])
        return clean
    elif isinstance(structure, list):
        return [create_clean_structure_for_description(item) for item in structure]
    return structure


def _node_page_indices(node: dict) -> list[int]:
    return list(range(node["start_index"] - 1, node["end_index"]))


def _overlap_indices(start_one_based: int, end_one_based: int,
                     overlap_pages: int, total_pages: int) -> tuple[list[int], list[int]]:
    if overlap_pages <= 0:
        return [], []
    pre_start = max(1, start_one_based - overlap_pages)
    pre = list(range(pre_start - 1, start_one_based - 1)) if start_one_based > 1 else []
    post_end = min(total_pages, end_one_based + overlap_pages)
    post = list(range(end_one_based, post_end)) if end_one_based < total_pages else []
    return pre, post


async def toc_detector_single_page(
    page_idx: int, *, opt: BuildOpt, call_llm: CallLlm,
) -> str:
    spec = _spec("toc_detector_single_page", opt,
                 page={"content": _page([page_idx])})
    data = await call_llm.parsed(opt.model_fast, spec, "toc_detection")
    return data.get("toc_detected", "no")


async def find_toc_pages(
    start_page_index: int, num_pages: int, opt: BuildOpt, *, call_llm: CallLlm,
) -> list[int]:
    last_page_is_yes = False
    toc_page_list: list[int] = []
    i = start_page_index

    while i < num_pages:
        if i >= opt.toc_check_page_num and not last_page_is_yes:
            break
        res = await toc_detector_single_page(i, opt=opt, call_llm=call_llm)
        if res == "yes":
            toc_page_list.append(i)
            last_page_is_yes = True
        elif res == "no" and last_page_is_yes:
            break
        i += 1

    return toc_page_list


async def detect_page_index(
    toc_page_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm,
) -> str:
    spec = _spec("detect_page_index", opt,
                 page={"toc_content": _page(toc_page_indices, transform_dots=True)})
    data = await call_llm.parsed(opt.model_fast, spec, "page_index_present")
    return data.get("page_index_given_in_toc", "no")


async def toc_extractor(
    toc_page_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm,
) -> dict:
    has_page_index = await detect_page_index(toc_page_indices, opt=opt, call_llm=call_llm)
    return {"toc_page_indices": toc_page_indices, "page_index_given_in_toc": has_page_index}


async def check_if_toc_transformation_is_complete(
    toc_page_indices: list[int], last_complete: str,
    *, opt: BuildOpt, call_llm: CallLlm,
) -> str:
    # Template's `toc` kwarg gets the LLM's partial JSON; `content` gets the raw TOC pages.
    spec = _spec(
        "check_if_toc_transformation_is_complete", opt,
        small={"toc": last_complete},
        page={"content": _page(toc_page_indices, transform_dots=True)},
    )
    data = await call_llm.parsed(opt.model_fast, spec, "toc_completion")
    return data.get("completed", "no")


async def toc_transformer(
    toc_page_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec("toc_transformer", opt,
                 page={"toc_content": _page(toc_page_indices, transform_dots=True)})
    result = await call_llm(opt.model, spec, json_mode=True)
    last_complete = result.content
    finished = result.finish_reason != "length"

    if_complete = await check_if_toc_transformation_is_complete(
        toc_page_indices, last_complete, opt=opt, call_llm=call_llm,
    )
    if if_complete == "yes" and finished:
        toc_list = _parse_toc_assembly(last_complete)
        if toc_list:
            return convert_page_to_int(toc_list)

    last_complete = _get_json_content(last_complete)

    for _ in range(5):
        position = last_complete.rfind("}")
        if position != -1:
            last_complete = last_complete[: position + 2]

        cont_spec = _spec(
            "toc_transformer_continue", opt,
            small={"last_complete": last_complete},
            page={"toc_content": _page(toc_page_indices, transform_dots=True)},
        )
        cont_result = await call_llm(opt.model, cont_spec, json_mode=True)
        new_complete = cont_result.content
        finished = cont_result.finish_reason != "length"
        if new_complete.startswith("```json"):
            new_complete = _get_json_content(new_complete)
            last_complete += new_complete

        if_complete = await check_if_toc_transformation_is_complete(
            toc_page_indices, last_complete, opt=opt, call_llm=call_llm,
        )
        if if_complete == "yes" and finished:
            break

    toc_list = _parse_toc_assembly(last_complete)
    if not toc_list:
        log.error("toc_transformer: could not extract TOC list from LLM response")
        return []
    return convert_page_to_int(toc_list)


async def toc_index_extractor(
    toc, content_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec(
        "toc_index_extractor", opt,
        small={"toc": toc},
        page={"content": _page(content_indices, wrap="physical_index")},
    )
    data = await call_llm.parsed(opt.model, spec, "toc_list")
    return data.get("toc", [])


async def add_page_number_to_toc(
    part_indices: list[int], structure, *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec(
        "add_page_number_to_toc", opt,
        small={"structure": structure},
        page={"part": _page(part_indices, wrap="physical_index")},
    )
    data = await call_llm.parsed(opt.model, spec, "toc_list")
    items = data.get("toc", [])
    for item in items:
        item.pop("start", None)
    return items


async def generate_toc_init(
    part_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm, toc_hint=None,
):
    if toc_hint:
        spec = _spec(
            "generate_toc_init_with_hint", opt,
            small={"toc_hint": toc_hint},
            page={"part": _page(part_indices, wrap="physical_index")},
        )
    else:
        spec = _spec(
            "generate_toc_init", opt,
            page={"part": _page(part_indices, wrap="physical_index")},
        )
    # instructor validates+repairs truncation; the previous finish_reason=="length"
    # gate is subsumed — a genuinely un-repairable response propagates as an exception.
    return await call_llm.parsed(opt.model, spec, "toc_list")


async def generate_toc_continue(
    toc_content, part_indices: list[int], *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec(
        "generate_toc_continue", opt,
        small={"toc_content": toc_content},
        page={"part": _page(part_indices, wrap="physical_index")},
    )
    return await call_llm.parsed(opt.model, spec, "toc_list")


async def process_no_toc(
    page_indices: list[int], token_counts: list[int],
    *, opt: BuildOpt, call_llm: CallLlm,
    toc_hint=None,
):
    overhead = physical_index_overhead_tokens()
    group_indices = page_list_to_group_indices(
        page_indices, token_counts,
        max_tokens=10000, overlap_page=1, per_page_overhead_tokens=overhead,
    )
    log.debug("process_no_toc: %d group(s)%s", len(group_indices),
              f" with TOC hint ({len(toc_hint)} chars)" if toc_hint else "")

    # Prompts wrap the array in {"toc": [...]} for vLLM's strict json_object root requirement.
    toc = (await generate_toc_init(
        group_indices[0], opt=opt, call_llm=call_llm, toc_hint=toc_hint
    ))["toc"]
    for group in group_indices[1:]:
        additional = (await generate_toc_continue(
            toc, group, opt=opt, call_llm=call_llm
        ))["toc"]
        toc.extend(additional)

    log.debug("generate_toc result: %s", toc)
    return convert_physical_index_to_int(toc)


async def process_toc_no_page_numbers(
    toc_page_indices: list[int], page_indices: list[int], token_counts: list[int],
    *, opt: BuildOpt, call_llm: CallLlm,
):
    toc_structured = await toc_transformer(toc_page_indices, opt=opt, call_llm=call_llm)
    log.debug("toc_transformer: %s", toc_structured)

    overhead = physical_index_overhead_tokens()
    group_indices = page_list_to_group_indices(
        page_indices, token_counts,
        max_tokens=10000, overlap_page=1, per_page_overhead_tokens=overhead,
    )
    log.debug("process_toc_no_page_numbers: %d group(s)", len(group_indices))

    toc_with_pages = copy.deepcopy(toc_structured)
    for group in group_indices:
        toc_with_pages = await add_page_number_to_toc(
            group, toc_with_pages, opt=opt, call_llm=call_llm,
        )

    log.debug("add_page_number_to_toc: %s", toc_with_pages)
    return convert_physical_index_to_int(toc_with_pages)


async def process_none_page_numbers(
    toc_items, total_pages: int, *, opt: BuildOpt, call_llm: CallLlm, start_index=1,
):
    """Fill missing physical_index values by searching between known anchors."""
    for i, item in enumerate(toc_items):
        if "physical_index" not in item:
            prev = 0
            for j in range(i - 1, -1, -1):
                if toc_items[j].get("physical_index") is not None:
                    prev = toc_items[j]["physical_index"]
                    break

            nxt = -1
            for j in range(i + 1, len(toc_items)):
                if toc_items[j].get("physical_index") is not None:
                    nxt = toc_items[j]["physical_index"]
                    break

            search_pages = []
            for page_index in range(prev, nxt + 1):
                li = page_index - start_index
                if 0 <= li < total_pages:
                    search_pages.append(li)

            item_copy = copy.deepcopy(item)
            item_copy.pop("page", None)
            result = await add_page_number_to_toc(
                search_pages, item_copy, opt=opt, call_llm=call_llm,
            )
            if (isinstance(result, list) and result
                    and isinstance(result[0].get("physical_index"), str)
                    and result[0]["physical_index"].startswith("<physical_index")):
                item["physical_index"] = int(
                    result[0]["physical_index"].split("_")[-1].rstrip(">").strip()
                )
                item.pop("page", None)

    return toc_items


def extract_matching_page_pairs(toc_page, toc_physical_index, start_page_index):
    pairs = []
    for phy_item in toc_physical_index:
        for page_item in toc_page:
            if phy_item.get("title") == page_item.get("title"):
                pi = phy_item.get("physical_index")
                if pi is not None and int(pi) >= start_page_index:
                    pairs.append({
                        "title": phy_item["title"],
                        "page": page_item["page"],
                        "physical_index": pi,
                    })
    return pairs


def calculate_page_offset(pairs):
    diffs = []
    for p in pairs:
        try:
            diffs.append(p["physical_index"] - p["page"])
        except (KeyError, TypeError):
            continue
    if not diffs:
        return None
    counts = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    return max(counts.items(), key=lambda x: x[1])[0]


def add_page_offset_to_toc_json(data, offset):
    for item in data:
        if item.get("page") is not None and isinstance(item["page"], int):
            item["physical_index"] = item["page"] + offset
            del item["page"]
    return data


async def process_toc_with_page_numbers(
    toc_page_indices: list[int], total_pages: int,
    *, opt: BuildOpt, call_llm: CallLlm,
    toc_check_page_num=None,
):
    toc_with_pages = await toc_transformer(toc_page_indices, opt=opt, call_llm=call_llm)
    log.debug("toc_with_page_number: %s", toc_with_pages)

    toc_no_pages = remove_page_number(copy.deepcopy(toc_with_pages))

    start_page = toc_page_indices[-1] + 1
    end = min(start_page + (toc_check_page_num or 25), total_pages)
    main_content_indices = list(range(start_page, end))

    toc_with_physical = await toc_index_extractor(
        toc_no_pages, main_content_indices, opt=opt, call_llm=call_llm,
    )
    log.debug("toc_with_physical_index: %s", toc_with_physical)

    toc_with_physical = convert_physical_index_to_int(toc_with_physical)

    pairs = extract_matching_page_pairs(toc_with_pages, toc_with_physical, start_page)
    log.debug("matching_pairs: %s", pairs)

    offset = calculate_page_offset(pairs)
    log.debug("offset: %s", offset)

    toc_with_pages = add_page_offset_to_toc_json(toc_with_pages, offset)
    toc_with_pages = await process_none_page_numbers(
        toc_with_pages, total_pages, opt=opt, call_llm=call_llm,
    )

    log.debug("final toc_with_page_number: %s", toc_with_pages)
    return toc_with_pages


async def check_toc(total_pages: int, opt: BuildOpt, *, call_llm: CallLlm):
    # TOC search bounded to first third.
    capped = opt.model_copy(update={
        "toc_check_page_num": min(opt.toc_check_page_num, max(3, total_pages // 3))
    })

    toc_page_list = await find_toc_pages(
        start_page_index=0, num_pages=total_pages, opt=capped, call_llm=call_llm,
    )
    if not toc_page_list:
        return {"toc_page_indices": [], "toc_page_list": [], "page_index_given_in_toc": "no"}

    toc_json = await toc_extractor(toc_page_list, opt=capped, call_llm=call_llm)
    if toc_json["page_index_given_in_toc"] == "yes":
        return {
            "toc_page_indices": toc_json["toc_page_indices"],
            "toc_page_list": toc_page_list,
            "page_index_given_in_toc": "yes",
        }

    current_start = toc_page_list[-1] + 1
    while (toc_json["page_index_given_in_toc"] == "no"
           and current_start < total_pages
           and current_start < capped.toc_check_page_num):
        additional = await find_toc_pages(
            start_page_index=current_start, num_pages=total_pages,
            opt=capped, call_llm=call_llm,
        )
        if not additional:
            break
        additional_json = await toc_extractor(additional, opt=capped, call_llm=call_llm)
        if additional_json["page_index_given_in_toc"] == "yes":
            return {
                "toc_page_indices": additional_json["toc_page_indices"],
                "toc_page_list": additional,
                "page_index_given_in_toc": "yes",
            }
        current_start = additional[-1] + 1

    return {
        "toc_page_indices": toc_json["toc_page_indices"],
        "toc_page_list": toc_page_list,
        "page_index_given_in_toc": "no",
    }


async def check_title_appearance(
    item, *, opt: BuildOpt, call_llm: CallLlm,
):
    title = item["title"]
    if "physical_index" not in item or item["physical_index"] is None:
        return {"list_index": item.get("list_index"), "answer": "no",
                "title": title, "page_number": None}

    page_number = item["physical_index"]
    spec = _spec(
        "check_title_appearance", opt,
        small={"title": title},
        page={"page_text": _page([page_number - 1])},
    )
    data = await call_llm.parsed(opt.model_fast, spec, "title_appearance")
    answer = data.get("answer", "no")
    return {"list_index": item.get("list_index"), "answer": answer,
            "title": title, "page_number": page_number}


async def check_title_appearance_in_start(
    title: str, page_idx: int, *, opt: BuildOpt, call_llm: CallLlm,
) -> str:
    spec = _spec(
        "check_title_appearance_in_start", opt,
        small={"title": title},
        page={"page_text": _page([page_idx])},
    )
    data = await call_llm.parsed(opt.model_fast, spec, "title_starts_section")
    return data.get("start_begin", "no")


async def check_title_appearance_in_start_concurrent(
    structure, *, opt: BuildOpt, call_llm: CallLlm,
):
    for item in structure:
        if item.get("physical_index") is None:
            item["appear_start"] = "no"

    tasks = []
    valid_items = []
    for item in structure:
        if item.get("physical_index") is not None:
            tasks.append(
                check_title_appearance_in_start(
                    item["title"], item["physical_index"] - 1,
                    opt=opt, call_llm=call_llm,
                )
            )
            valid_items.append(item)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item, result in zip(valid_items, results):
        if isinstance(result, Exception):
            item["appear_start"] = "no"
        else:
            item["appear_start"] = result

    return structure


async def verify_toc(
    slice_length: int, list_result, *,
    opt: BuildOpt, call_llm: CallLlm, rng: random.Random,
    N=None,
):
    last_pi = None
    for item in reversed(list_result):
        if item.get("physical_index") is not None:
            last_pi = item["physical_index"]
            break

    # Abandon if TOC only covers the first half of the slice.
    if last_pi is None or last_pi < slice_length / 2:
        return 0, []

    if N is None:
        sample_indices = range(len(list_result))
    else:
        N = min(N, len(list_result))
        sample_indices = rng.sample(range(len(list_result)), N)

    indexed = []
    for idx in sample_indices:
        item = list_result[idx]
        if item.get("physical_index") is not None:
            item_copy = item.copy()
            item_copy["list_index"] = idx
            indexed.append(item_copy)

    results = await asyncio.gather(*[
        check_title_appearance(item, opt=opt, call_llm=call_llm)
        for item in indexed
    ])

    correct = sum(1 for r in results if r["answer"] == "yes")
    incorrect = [r for r in results if r["answer"] != "yes"]
    accuracy = correct / len(results) if results else 0
    return accuracy, incorrect


async def single_toc_item_index_fixer(
    section_title: str, content_indices: list[int],
    *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec(
        "single_toc_item_index_fixer", opt,
        small={"section_title": section_title},
        page={"content": _page(content_indices, wrap="physical_index")},
    )
    data = await call_llm.parsed(opt.model, spec, "physical_index_fix")
    return convert_physical_index_to_int(data.get("physical_index", ""))


async def fix_incorrect_toc(
    toc, slice_length: int, incorrect_results,
    *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1,
):
    # start_index is the 1-based physical_index of page_indices[0] (= node.start in recursion).
    incorrect_indices = {r["list_index"] for r in incorrect_results}
    slice_end_pi = start_index + slice_length - 1

    async def process_item(incorrect_item):
        list_index = incorrect_item["list_index"]
        if list_index < 0 or list_index >= len(toc):
            return {"list_index": list_index, "title": incorrect_item["title"],
                    "physical_index": None, "is_valid": False}

        prev_correct = start_index - 1
        for j in range(list_index - 1, -1, -1):
            if j not in incorrect_indices and 0 <= j < len(toc):
                pi = toc[j].get("physical_index")
                if pi is not None:
                    prev_correct = pi
                    break

        next_correct = slice_end_pi
        for j in range(list_index + 1, len(toc)):
            if j not in incorrect_indices and 0 <= j < len(toc):
                pi = toc[j].get("physical_index")
                if pi is not None:
                    next_correct = pi
                    break

        content_indices = []
        for page_index in range(prev_correct, next_correct + 1):
            if start_index <= page_index <= slice_end_pi:
                content_indices.append(page_index - 1)

        pi_int = await single_toc_item_index_fixer(
            incorrect_item["title"], content_indices, opt=opt, call_llm=call_llm,
        )

        check_item = incorrect_item.copy()
        check_item["physical_index"] = pi_int
        check_result = await check_title_appearance(
            check_item, opt=opt, call_llm=call_llm,
        )

        return {
            "list_index": list_index,
            "title": incorrect_item["title"],
            "physical_index": pi_int,
            "is_valid": check_result["answer"] == "yes",
        }

    results = await asyncio.gather(
        *[process_item(item) for item in incorrect_results],
        return_exceptions=True,
    )
    results = [r for r in results if not isinstance(r, Exception)]

    invalid = []
    for result in results:
        if result["is_valid"]:
            idx = result["list_index"]
            if 0 <= idx < len(toc):
                toc[idx]["physical_index"] = result["physical_index"]
            else:
                invalid.append(result)
        else:
            invalid.append(result)

    return toc, invalid


async def fix_incorrect_toc_with_retries(
    toc, slice_length: int, incorrect_results,
    *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1, max_attempts=3,
):
    current_toc = toc
    current_incorrect = incorrect_results

    for _ in range(max_attempts):
        if not current_incorrect:
            break
        current_toc, current_incorrect = await fix_incorrect_toc(
            current_toc, slice_length, current_incorrect,
            opt=opt, call_llm=call_llm, start_index=start_index,
        )

    return current_toc, current_incorrect


async def meta_processor(
    page_indices: list[int], token_counts: list[int],
    *,
    opt: BuildOpt, call_llm: CallLlm, rng: random.Random,
    mode=None, toc_page_indices=None, toc_page_list=None, start_index=1,
):
    # page_indices: global 0-based slice. start_index: 1-based physical_index of page_indices[0].
    num_active = len(page_indices)

    if mode == "process_toc_with_page_numbers":
        toc = await process_toc_with_page_numbers(
            toc_page_indices, len(token_counts),
            opt=opt, call_llm=call_llm,
            toc_check_page_num=opt.toc_check_page_num,
        )
    elif mode == "process_toc_no_page_numbers":
        toc = await process_toc_no_page_numbers(
            toc_page_indices, page_indices, token_counts,
            opt=opt, call_llm=call_llm,
        )
    else:
        toc = await process_no_toc(
            page_indices, token_counts,
            opt=opt, call_llm=call_llm, toc_hint=None,
        )

    toc = [item for item in toc if item.get("physical_index") is not None]
    toc = validate_and_truncate_physical_indices(
        toc, num_active, start_index=start_index,
    )

    accuracy, incorrect = await verify_toc(
        num_active, toc,
        opt=opt, call_llm=call_llm, rng=rng,
    )

    log.debug("meta_processor mode=%s accuracy=%.2f incorrect=%d",
              mode, accuracy, len(incorrect))

    if accuracy == 1.0 and not incorrect:
        return toc
    if accuracy > 0.6 and incorrect:
        toc, _ = await fix_incorrect_toc_with_retries(
            toc, num_active, incorrect,
            opt=opt, call_llm=call_llm,
            start_index=start_index, max_attempts=3,
        )
        return toc

    # Fallback cascade
    if mode == "process_toc_with_page_numbers":
        return await meta_processor(
            page_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_no_page_numbers",
            toc_page_indices=toc_page_indices, toc_page_list=toc_page_list,
            start_index=start_index,
        )
    elif mode == "process_toc_no_page_numbers":
        return await meta_processor(
            page_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc",
            toc_page_indices=toc_page_indices, start_index=start_index,
        )
    else:
        log.warning(
            "Tree building: all processing modes failed verification "
            "(accuracy=%.2f). Returning best-effort TOC (%d items).",
            accuracy, len(toc),
        )
        if toc:
            return toc
        return [{
            "structure": "1",
            "title": "Document",
            "physical_index": start_index,
        }]


_MAX_SUBDIVISION_DEPTH = 3


async def process_large_node_recursively(
    node, token_counts: list[int],
    *,
    opt: BuildOpt, call_llm: CallLlm, rng: random.Random,
    _depth=0,
):
    if _depth >= _MAX_SUBDIVISION_DEPTH:
        log.warning(
            "Max subdivision depth (%d) reached for node '%s' (pages %d–%d).",
            _MAX_SUBDIVISION_DEPTH, node.get("title", "?"),
            node["start_index"], node["end_index"],
        )
        return node

    parent_span = node["end_index"] - node["start_index"]
    node_indices = _node_page_indices(node)
    token_num = sum(token_counts[i] for i in node_indices)

    if (parent_span > opt.max_page_num_each_node
            and token_num >= opt.max_token_num_each_node):

        sub_toc = await meta_processor(
            node_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc", start_index=node["start_index"],
        )
        sub_toc = await check_title_appearance_in_start_concurrent(
            sub_toc, opt=opt, call_llm=call_llm,
        )

        valid = [item for item in sub_toc if item.get("physical_index") is not None]

        if valid and node["title"].strip() == valid[0]["title"].strip():
            node["nodes"] = post_processing(valid[1:], node["end_index"])
            node["end_index"] = valid[1]["start_index"] if len(valid) > 1 else node["end_index"]
        else:
            node["nodes"] = post_processing(valid, node["end_index"])
            node["end_index"] = valid[0]["start_index"] if valid else node["end_index"]

        # Stop if any child's span >= parent's — degenerate LLM split.
        if node.get("nodes"):
            stalled = [
                c for c in node["nodes"]
                if c["end_index"] - c["start_index"] >= parent_span
            ]
            if stalled:
                log.warning(
                    "Subdivision made no progress for node '%s' (pages %d–%d).",
                    node.get("title", "?"), node["start_index"], node["end_index"],
                )
                return node

    if node.get("nodes"):
        await asyncio.gather(*[
            process_large_node_recursively(
                child, token_counts,
                opt=opt, call_llm=call_llm, rng=rng, _depth=_depth + 1,
            )
            for child in node["nodes"]
        ])

    return node


def _node_summary_page_spec(node: dict, total_pages: int, overlap_pages: int) -> PageRangeSpec:
    indices = _node_page_indices(node)
    pre, post = _overlap_indices(
        node["start_index"], node["end_index"], overlap_pages, total_pages,
    )
    return _page(indices, overlap_pre=pre, overlap_post=post)


def _node_verify_page_spec(node: dict, total_pages: int, overlap_pages: int) -> PageRangeSpec:
    # Same span as the summary, but section-only — for fidelity checks.
    indices = _node_page_indices(node)
    pre, post = _overlap_indices(
        node["start_index"], node["end_index"], overlap_pages, total_pages,
    )
    return _page(indices, overlap_pre=pre, overlap_post=post, extract_section_only=True)


async def generate_node_summary(
    node, total_pages: int, *, opt: BuildOpt, call_llm: CallLlm,
):
    spec = _spec(
        "generate_node_summary", opt,
        page={"node_text": _node_summary_page_spec(
            node, total_pages, opt.summary_overlap_pages,
        )},
    )
    result = await call_llm(opt.model_fast, spec)
    return result.content


async def generate_summaries_for_structure(
    structure, total_pages: int, *, opt: BuildOpt, call_llm: CallLlm,
):
    nodes = structure_to_list(structure)
    summaries = await asyncio.gather(*[
        generate_node_summary(node, total_pages, opt=opt, call_llm=call_llm)
        for node in nodes
    ])
    for node, summary in zip(nodes, summaries):
        node["summary"] = summary
    return structure


async def verify_summaries_for_structure(
    structure, total_pages: int, *, opt: BuildOpt, call_llm: CallLlm, max_retries: int = 1,
):
    nodes = structure_to_list(structure)

    async def verify_one(node):
        if not node.get("summary"):
            return
        verify_spec = _spec(
            "verify_node_summary", opt,
            small={"title": node.get("title", ""), "summary": node["summary"]},
            page={"section_text": _node_verify_page_spec(
                node, total_pages, opt.summary_overlap_pages,
            )},
        )
        try:
            data = await call_llm.parsed(opt.model_fast, verify_spec, "summary_verdict")
        except Exception as exc:
            log.warning(f"verify_node_summary failed for '{node.get('title')}': {exc}")
            return

        faithful = data.get("faithful", "no")
        missed = data.get("missed_topics") or []
        if faithful == "yes" and not missed:
            return
        if not missed:
            return

        for _ in range(max_retries):
            regen_spec = _spec(
                "regenerate_summary_with_missed_topics", opt,
                small={"prior_summary": node["summary"], "missed_topics": missed},
                page={"node_text": _node_summary_page_spec(
                    node, total_pages, opt.summary_overlap_pages,
                )},
            )
            try:
                regen_result = await call_llm(opt.model_fast, regen_spec)
                node["summary"] = regen_result.content
            except Exception as exc:
                log.warning(f"regenerate_summary failed for '{node.get('title')}': {exc}")
                return

    await asyncio.gather(*[verify_one(n) for n in nodes])
    return structure


async def generate_doc_description(structure, *, opt: BuildOpt, call_llm: CallLlm):
    spec = _spec("generate_doc_description", opt,
                 small={"structure": structure})
    result = await call_llm(opt.model, spec)
    return result.content


def _dicts_to_tree_nodes(nodes: list[dict]) -> list[TreeNode]:
    result = []
    for n in nodes:
        children = _dicts_to_tree_nodes(n.get("nodes", []) or [])
        result.append(TreeNode(
            title=n.get("title", ""),
            node_id=n.get("node_id", "0000"),
            start_index=n.get("start_index", 1),
            end_index=n.get("end_index", 1),
            summary=n.get("summary"),
            nodes=children,
        ))
    return result


async def build_tree(
    token_counts: list[int],
    opt: BuildOpt,
    *,
    call_llm: CallLlm,
    rng: random.Random,
    paper_id: str,
    pdf_path: str,
) -> DocumentTree:
    total_pages = len(token_counts)
    total_tokens = sum(token_counts)
    log.info("Pages: %d, tokens: %d", total_pages, total_tokens)

    check_result = await check_toc(total_pages, opt, call_llm=call_llm)
    log.debug("check_toc_result: %s", check_result)

    has_toc = bool(check_result.get("toc_page_indices"))
    full_indices = list(range(total_pages))

    if has_toc and check_result["page_index_given_in_toc"] == "yes":
        toc = await meta_processor(
            full_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_with_page_numbers",
            start_index=1,
            toc_page_indices=check_result["toc_page_indices"],
            toc_page_list=check_result["toc_page_list"],
        )
    elif has_toc:
        toc = await meta_processor(
            full_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_no_page_numbers",
            start_index=1,
            toc_page_indices=check_result["toc_page_indices"],
            toc_page_list=check_result["toc_page_list"],
        )
    else:
        toc = await meta_processor(
            full_indices, token_counts, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc", start_index=1,
        )

    toc = add_preface_if_needed(toc)
    toc = await check_title_appearance_in_start_concurrent(
        toc, opt=opt, call_llm=call_llm,
    )

    valid_toc = [item for item in toc if item.get("physical_index") is not None]
    tree_nodes = post_processing(valid_toc, total_pages)
    await asyncio.gather(*[
        process_large_node_recursively(
            node, token_counts, opt=opt, call_llm=call_llm, rng=rng,
        )
        for node in tree_nodes
    ])

    if opt.if_add_node_id == "yes":
        write_node_id(tree_nodes)

    if opt.if_add_node_summary == "yes":
        await generate_summaries_for_structure(
            tree_nodes, total_pages, opt=opt, call_llm=call_llm,
        )

        if opt.verify_summaries:
            await verify_summaries_for_structure(
                tree_nodes, total_pages, opt=opt, call_llm=call_llm,
            )

    doc_description = None
    if opt.if_add_doc_description == "yes":
        clean = create_clean_structure_for_description(tree_nodes)
        doc_description = await generate_doc_description(clean, opt=opt, call_llm=call_llm)

    tree_nodes = format_structure(
        tree_nodes,
        order=["title", "node_id", "start_index", "end_index", "summary", "nodes"],
    )

    root_nodes = _dicts_to_tree_nodes(tree_nodes)

    return DocumentTree(
        paper_id=paper_id,
        pdf_path=pdf_path,
        total_pages=total_pages,
        doc_description=doc_description,
        root_nodes=root_nodes,
    )
