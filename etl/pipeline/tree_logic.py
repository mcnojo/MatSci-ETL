"""Pure document-tree orchestration.

Public entry: `build_tree(pages, opt, *, call_llm, rng, paper_id, pdf_path)`.

All LLM I/O is injected via the `call_llm` parameter, all randomness via `rng`.
The module imports only pure stdlib + tiktoken + pydantic, so it is safe to
run inside the Temporal workflow sandbox.

Callers:
- prod/workflows/process_pdf.py — runs inside the workflow; the call_llm
  closure schedules `llm_text_call_activity` per call. `rng = workflow.random()`.
- tests/test_tree_logic.py — passes an inline fake call_llm + a seeded
  `random.Random` for reproducibility.
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

from shared.prompts.etl import get_prompt
from shared.schemas import DocumentTree, TreeNode

log = logging.getLogger("tree_logic")


# CallLlm seam

class LlmResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    content: str
    finish_reason: str


class CallLlm(Protocol):
    async def __call__(
        self, model: str, prompt: str, *,
        json_mode: bool = False, temperature: float = 0.0,
    ) -> LlmResult: ...


# Build options

class BuildOpt(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str                   # strong model
    model_fast: str              # fast model — TOC detection, verification, summary
    prompt_style: str            # "local" | "upstream"
    toc_check_page_num: int
    max_page_num_each_node: int
    max_token_num_each_node: int
    if_add_node_id: str          # "yes" | "no"
    if_add_node_summary: str     # "yes" | "no"
    if_add_doc_description: str  # "yes" | "no"
    summary_overlap_pages: int
    verify_summaries: bool


def build_opt_from_config(config: dict) -> BuildOpt:
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
        toc_check_page_num=tree_cfg["toc_check_pages"],
        max_page_num_each_node=tree_cfg["max_pages_per_node"],
        max_token_num_each_node=tree_cfg["max_tokens_per_node"],
        if_add_node_id="yes" if tree_cfg.get("add_node_id", True) else "no",
        if_add_node_summary="yes" if tree_cfg.get("add_node_summary", True) else "no",
        if_add_doc_description="yes" if tree_cfg.get("add_doc_description", False) else "no",
        summary_overlap_pages=tree_cfg.get("summary_overlap_pages", 1),
        verify_summaries=tree_cfg.get("verify_summaries", True),
    )


# Token counting (pure tiktoken)

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_enc.encode(text))


# JSON helpers

def _get_json_content(response: str) -> str:
    start_idx = response.find("```json")
    if start_idx != -1:
        response = response[start_idx + 7:]
    end_idx = response.rfind("```")
    if end_idx != -1:
        response = response[:end_idx]
    return response.strip()


def _strip_thinking(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def extract_json(content: str) -> dict | list:
    if not content or not content.strip():
        log.error("extract_json: received empty response from LLM")
        return {}

    content = _strip_thinking(content)
    if not content.strip():
        log.error("extract_json: response was only thinking tokens, no content")
        return {}

    json_content = ""
    try:
        start_idx = content.find("```json")
        if start_idx != -1:
            json_content = content[start_idx + 7:content.rfind("```")].strip()
        else:
            json_content = content.strip()

        json_content = json_content.replace("None", "null")
        json_content = json_content.replace("\n", " ").replace("\r", " ")
        json_content = " ".join(json_content.split())
        return json.loads(json_content)
    except json.JSONDecodeError:
        try:
            json_content = json_content.replace(",]", "]").replace(",}", "}")
            return json.loads(json_content)
        except Exception:
            pass

        try:
            match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", content)
            if match:
                return json.loads(match.group(1))
        except Exception:
            pass

        log.error("extract_json: failed to parse. First 500 chars:\n%s", content[:500])
        return {}
    except Exception as e:
        log.error("extract_json: unexpected error: %s", e)
        return {}


# Tree-structure utilities (pure)

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


def page_list_to_group_text(page_contents, token_lengths, max_tokens=10000, overlap_page=1):
    num_tokens = sum(token_lengths)
    if num_tokens <= max_tokens:
        return ["".join(page_contents)]

    expected_parts = math.ceil(num_tokens / max_tokens)
    avg_tokens = math.ceil(((num_tokens / expected_parts) + max_tokens) / 2)

    subsets = []
    current_subset = []
    current_count = 0

    for i, (content, tokens) in enumerate(zip(page_contents, token_lengths)):
        if current_count + tokens > avg_tokens:
            subsets.append("".join(current_subset))
            overlap_start = max(i - overlap_page, 0)
            current_subset = list(page_contents[overlap_start:i])
            current_count = sum(token_lengths[overlap_start:i])
        current_subset.append(content)
        current_count += tokens

    if current_subset:
        subsets.append("".join(current_subset))

    return subsets


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
        return tree

    for node in structure:
        node.pop("appear_start", None)
        node.pop("physical_index", None)
    return structure


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


def get_text_of_pdf_pages(pdf_pages, start_page, end_page):
    return "".join(pdf_pages[i][0] for i in range(start_page - 1, end_page))


def add_node_text(node, pdf_pages, overlap_pages: int = 0):
    """Attach the page-range text to each node, optionally with overlap context.

    When overlap_pages > 0, the node's text is wrapped:
        <<<context-before>>>{prev N pages}
        <<<section-content>>>{the section}
        <<<context-after>>>{next N pages}
    Summarizers are instructed to summarize only the section-content block.
    """
    if isinstance(node, dict):
        s, e = node["start_index"], node["end_index"]
        total = len(pdf_pages)
        if overlap_pages > 0:
            pre_start = max(1, s - overlap_pages)
            post_end = min(total, e + overlap_pages)
            pre = (get_text_of_pdf_pages(pdf_pages, pre_start, s - 1)
                   if s > 1 and pre_start < s else "")
            core = get_text_of_pdf_pages(pdf_pages, s, e)
            post = (get_text_of_pdf_pages(pdf_pages, e + 1, post_end)
                    if e < total and post_end > e else "")
            node["text"] = (
                f"<<<context-before>>>\n{pre}\n"
                f"<<<section-content>>>\n{core}\n"
                f"<<<context-after>>>\n{post}"
            )
        else:
            node["text"] = get_text_of_pdf_pages(pdf_pages, s, e)
        if "nodes" in node:
            add_node_text(node["nodes"], pdf_pages, overlap_pages)
    elif isinstance(node, list):
        for item in node:
            add_node_text(item, pdf_pages, overlap_pages)


def remove_structure_text(data):
    if isinstance(data, dict):
        data.pop("text", None)
        if "nodes" in data:
            remove_structure_text(data["nodes"])
    elif isinstance(data, list):
        for item in data:
            remove_structure_text(item)


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


def _extract_section_content(text: str) -> str:
    """Pull just the <<<section-content>>> block out of overlap-wrapped text."""
    start_marker = "<<<section-content>>>"
    end_marker = "<<<context-after>>>"
    si = text.find(start_marker)
    if si == -1:
        return text
    si += len(start_marker)
    ei = text.find(end_marker, si)
    return text[si:ei].strip() if ei != -1 else text[si:].strip()


def _extract_toc_list(parsed: dict | list) -> list | None:
    """Pull the TOC list from whatever shape the LLM returned."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("table_of_contents", "toc", "contents"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        if len(parsed) == 1:
            val = next(iter(parsed.values()))
            if isinstance(val, list):
                return val
    return None


# TOC detection & extraction

async def toc_detector_single_page(content: str, *, opt: BuildOpt, call_llm: CallLlm) -> str:
    prompt = get_prompt("toc_detector_single_page", opt.prompt_style, content=content)
    result = await call_llm(opt.model_fast, prompt, json_mode=True)
    return extract_json(result.content).get("toc_detected", "no")


async def find_toc_pages(start_page_index, page_list, opt: BuildOpt, *, call_llm: CallLlm):
    last_page_is_yes = False
    toc_page_list = []
    i = start_page_index

    while i < len(page_list):
        if i >= opt.toc_check_page_num and not last_page_is_yes:
            break
        res = await toc_detector_single_page(page_list[i][0], opt=opt, call_llm=call_llm)
        if res == "yes":
            toc_page_list.append(i)
            last_page_is_yes = True
        elif res == "no" and last_page_is_yes:
            break
        i += 1

    return toc_page_list


async def detect_page_index(toc_content: str, *, opt: BuildOpt, call_llm: CallLlm) -> str:
    prompt = get_prompt("detect_page_index", opt.prompt_style, toc_content=toc_content)
    result = await call_llm(opt.model_fast, prompt, json_mode=True)
    return extract_json(result.content).get("page_index_given_in_toc", "no")


async def toc_extractor(page_list, toc_page_list, *, opt: BuildOpt, call_llm: CallLlm):
    def transform_dots(text):
        text = re.sub(r"\.{5,}", ": ", text)
        text = re.sub(r"(?:\. ){5,}\.?", ": ", text)
        return text

    toc_content = ""
    for idx in toc_page_list:
        toc_content += page_list[idx][0]
    toc_content = transform_dots(toc_content)
    has_page_index = await detect_page_index(toc_content, opt=opt, call_llm=call_llm)
    return {"toc_content": toc_content, "page_index_given_in_toc": has_page_index}


async def check_if_toc_transformation_is_complete(
    content, toc, *, opt: BuildOpt, call_llm: CallLlm,
) -> str:
    prompt = get_prompt(
        "check_if_toc_transformation_is_complete", opt.prompt_style,
        content=content, toc=toc,
    )
    result = await call_llm(opt.model_fast, prompt, json_mode=True)
    return extract_json(result.content).get("completed", "no")


async def toc_transformer(toc_content, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt("toc_transformer", opt.prompt_style, toc_content=toc_content)
    result = await call_llm(opt.model, prompt, json_mode=True)
    last_complete = result.content
    finished = result.finish_reason != "length"

    if_complete = await check_if_toc_transformation_is_complete(
        toc_content, last_complete, opt=opt, call_llm=call_llm,
    )
    if if_complete == "yes" and finished:
        parsed = extract_json(last_complete)
        toc_list = _extract_toc_list(parsed)
        if toc_list is not None:
            return convert_page_to_int(toc_list)

    last_complete = _get_json_content(last_complete)

    for _ in range(5):
        position = last_complete.rfind("}")
        if position != -1:
            last_complete = last_complete[: position + 2]

        cont_prompt = get_prompt(
            "toc_transformer_continue", opt.prompt_style,
            toc_content=toc_content, last_complete=last_complete,
        )

        cont_result = await call_llm(opt.model, cont_prompt, json_mode=True)
        new_complete = cont_result.content
        finished = cont_result.finish_reason != "length"
        if new_complete.startswith("```json"):
            new_complete = _get_json_content(new_complete)
            last_complete += new_complete

        if_complete = await check_if_toc_transformation_is_complete(
            toc_content, last_complete, opt=opt, call_llm=call_llm,
        )
        if if_complete == "yes" and finished:
            break

    parsed = extract_json(last_complete)
    toc_list = _extract_toc_list(parsed)
    if toc_list is None:
        log.error("toc_transformer: could not extract TOC list from LLM response")
        return []
    return convert_page_to_int(toc_list)


async def toc_index_extractor(toc, content, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt("toc_index_extractor", opt.prompt_style, toc=toc, content=content)
    result = await call_llm(opt.model, prompt, json_mode=True)
    return extract_json(result.content)


# Page number assignment

async def add_page_number_to_toc(part, structure, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt(
        "add_page_number_to_toc", opt.prompt_style,
        part=part, structure=structure,
    )
    result = await call_llm(opt.model, prompt, json_mode=True)
    parsed = extract_json(result.content)

    for item in parsed:
        item.pop("start", None)
    return parsed


# No-TOC tree generation

async def generate_toc_init(part, *, opt: BuildOpt, call_llm: CallLlm, toc_hint=None):
    if toc_hint:
        prompt = get_prompt(
            "generate_toc_init_with_hint", opt.prompt_style,
            part=part, toc_hint=toc_hint,
        )
    else:
        prompt = get_prompt("generate_toc_init", opt.prompt_style, part=part)
    result = await call_llm(opt.model, prompt, json_mode=True)
    if result.finish_reason != "length":
        return extract_json(result.content)
    raise RuntimeError(f"generate_toc_init: finish_reason={result.finish_reason}")


async def generate_toc_continue(toc_content, part, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt(
        "generate_toc_continue", opt.prompt_style,
        toc_content=toc_content, part=part,
    )
    result = await call_llm(opt.model, prompt, json_mode=True)
    if result.finish_reason != "length":
        return extract_json(result.content)
    raise RuntimeError(f"generate_toc_continue: finish_reason={result.finish_reason}")


# TOC processing paths

async def process_no_toc(
    page_list, *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1, toc_hint=None,
):
    page_contents = []
    token_lengths = []
    for page_index in range(start_index, start_index + len(page_list)):
        text = (
            f"<physical_index_{page_index}>\n"
            f"{page_list[page_index - start_index][0]}\n"
            f"<physical_index_{page_index}>\n\n"
        )
        page_contents.append(text)
        token_lengths.append(count_tokens(text))

    group_texts = page_list_to_group_text(page_contents, token_lengths)
    log.debug("process_no_toc: %d group(s)%s", len(group_texts),
              f" with TOC hint ({len(toc_hint)} chars)" if toc_hint else "")

    toc = await generate_toc_init(group_texts[0], opt=opt, call_llm=call_llm, toc_hint=toc_hint)
    for group_text in group_texts[1:]:
        additional = await generate_toc_continue(toc, group_text, opt=opt, call_llm=call_llm)
        toc.extend(additional)

    log.debug("generate_toc result: %s", toc)
    return convert_physical_index_to_int(toc)


async def process_toc_no_page_numbers(
    toc_content, toc_page_list, page_list,
    *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1,
):
    toc_structured = await toc_transformer(toc_content, opt=opt, call_llm=call_llm)
    log.debug("toc_transformer: %s", toc_structured)

    page_contents = []
    token_lengths = []
    for page_index in range(start_index, start_index + len(page_list)):
        text = (
            f"<physical_index_{page_index}>\n"
            f"{page_list[page_index - start_index][0]}\n"
            f"<physical_index_{page_index}>\n\n"
        )
        page_contents.append(text)
        token_lengths.append(count_tokens(text))

    group_texts = page_list_to_group_text(page_contents, token_lengths)
    log.debug("process_toc_no_page_numbers: %d group(s)", len(group_texts))

    toc_with_pages = copy.deepcopy(toc_structured)
    for group_text in group_texts:
        toc_with_pages = await add_page_number_to_toc(
            group_text, toc_with_pages, opt=opt, call_llm=call_llm,
        )

    log.debug("add_page_number_to_toc: %s", toc_with_pages)
    return convert_physical_index_to_int(toc_with_pages)


async def process_none_page_numbers(
    toc_items, page_list, *, opt: BuildOpt, call_llm: CallLlm, start_index=1,
):
    """Fill in missing physical_index values by searching between known anchors."""
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

            page_contents = []
            for page_index in range(prev, nxt + 1):
                list_index = page_index - start_index
                if 0 <= list_index < len(page_list):
                    text = (
                        f"<physical_index_{page_index}>\n"
                        f"{page_list[list_index][0]}\n"
                        f"<physical_index_{page_index}>\n\n"
                    )
                    page_contents.append(text)

            item_copy = copy.deepcopy(item)
            item_copy.pop("page", None)
            result = await add_page_number_to_toc(
                page_contents, item_copy, opt=opt, call_llm=call_llm,
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
    toc_content, toc_page_list, page_list,
    *, opt: BuildOpt, call_llm: CallLlm,
    toc_check_page_num=None,
):
    toc_with_pages = await toc_transformer(toc_content, opt=opt, call_llm=call_llm)
    log.debug("toc_with_page_number: %s", toc_with_pages)

    toc_no_pages = remove_page_number(copy.deepcopy(toc_with_pages))

    start_page = toc_page_list[-1] + 1
    main_content = ""
    end = min(start_page + (toc_check_page_num or 25), len(page_list))
    for page_index in range(start_page, end):
        main_content += (
            f"<physical_index_{page_index + 1}>\n"
            f"{page_list[page_index][0]}\n"
            f"<physical_index_{page_index + 1}>\n\n"
        )

    toc_with_physical = await toc_index_extractor(
        toc_no_pages, main_content, opt=opt, call_llm=call_llm,
    )
    log.debug("toc_with_physical_index: %s", toc_with_physical)

    toc_with_physical = convert_physical_index_to_int(toc_with_physical)

    pairs = extract_matching_page_pairs(toc_with_pages, toc_with_physical, start_page)
    log.debug("matching_pairs: %s", pairs)

    offset = calculate_page_offset(pairs)
    log.debug("offset: %s", offset)

    toc_with_pages = add_page_offset_to_toc_json(toc_with_pages, offset)
    toc_with_pages = await process_none_page_numbers(
        toc_with_pages, page_list, opt=opt, call_llm=call_llm,
    )

    log.debug("final toc_with_page_number: %s", toc_with_pages)
    return toc_with_pages


async def check_toc(page_list, opt: BuildOpt, *, call_llm: CallLlm):
    # Cap TOC search to the first third of the document.
    capped = opt.model_copy(update={
        "toc_check_page_num": min(opt.toc_check_page_num, max(3, len(page_list) // 3))
    })

    toc_page_list = await find_toc_pages(
        start_page_index=0, page_list=page_list, opt=capped, call_llm=call_llm,
    )
    if not toc_page_list:
        return {"toc_content": None, "toc_page_list": [], "page_index_given_in_toc": "no"}

    toc_json = await toc_extractor(page_list, toc_page_list, opt=capped, call_llm=call_llm)
    if toc_json["page_index_given_in_toc"] == "yes":
        return {
            "toc_content": toc_json["toc_content"],
            "toc_page_list": toc_page_list,
            "page_index_given_in_toc": "yes",
        }

    current_start = toc_page_list[-1] + 1
    while (toc_json["page_index_given_in_toc"] == "no"
           and current_start < len(page_list)
           and current_start < capped.toc_check_page_num):
        additional = await find_toc_pages(
            start_page_index=current_start, page_list=page_list, opt=capped, call_llm=call_llm,
        )
        if not additional:
            break
        additional_json = await toc_extractor(
            page_list, additional, opt=capped, call_llm=call_llm,
        )
        if additional_json["page_index_given_in_toc"] == "yes":
            return {
                "toc_content": additional_json["toc_content"],
                "toc_page_list": additional,
                "page_index_given_in_toc": "yes",
            }
        current_start = additional[-1] + 1

    return {
        "toc_content": toc_json["toc_content"],
        "toc_page_list": toc_page_list,
        "page_index_given_in_toc": "no",
    }


# Verification & correction

async def check_title_appearance(
    item, page_list, *, opt: BuildOpt, call_llm: CallLlm, start_index=1,
):
    title = item["title"]
    if "physical_index" not in item or item["physical_index"] is None:
        return {"list_index": item.get("list_index"), "answer": "no",
                "title": title, "page_number": None}

    page_number = item["physical_index"]
    page_text = page_list[page_number - start_index][0]

    prompt = get_prompt(
        "check_title_appearance", opt.prompt_style,
        title=title, page_text=page_text,
    )
    result = await call_llm(opt.model_fast, prompt, json_mode=True)
    parsed = extract_json(result.content)
    answer = parsed.get("answer", "no")
    return {"list_index": item.get("list_index"), "answer": answer,
            "title": title, "page_number": page_number}


async def check_title_appearance_in_start(
    title, page_text, *, opt: BuildOpt, call_llm: CallLlm,
):
    prompt = get_prompt(
        "check_title_appearance_in_start", opt.prompt_style,
        title=title, page_text=page_text,
    )
    result = await call_llm(opt.model_fast, prompt, json_mode=True)
    return extract_json(result.content).get("start_begin", "no")


async def check_title_appearance_in_start_concurrent(
    structure, page_list, *, opt: BuildOpt, call_llm: CallLlm,
):
    for item in structure:
        if item.get("physical_index") is None:
            item["appear_start"] = "no"

    tasks = []
    valid_items = []
    for item in structure:
        if item.get("physical_index") is not None:
            page_text = page_list[item["physical_index"] - 1][0]
            tasks.append(
                check_title_appearance_in_start(
                    item["title"], page_text, opt=opt, call_llm=call_llm,
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
    page_list, list_result, *,
    opt: BuildOpt, call_llm: CallLlm, rng: random.Random,
    start_index=1, N=None,
):
    last_pi = None
    for item in reversed(list_result):
        if item.get("physical_index") is not None:
            last_pi = item["physical_index"]
            break

    if last_pi is None or last_pi < len(page_list) / 2:
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
        check_title_appearance(
            item, page_list, opt=opt, call_llm=call_llm, start_index=start_index,
        )
        for item in indexed
    ])

    correct = sum(1 for r in results if r["answer"] == "yes")
    incorrect = [r for r in results if r["answer"] != "yes"]
    accuracy = correct / len(results) if results else 0
    return accuracy, incorrect


async def single_toc_item_index_fixer(
    section_title, content, *, opt: BuildOpt, call_llm: CallLlm,
):
    prompt = get_prompt(
        "single_toc_item_index_fixer", opt.prompt_style,
        section_title=section_title, content=content,
    )
    result = await call_llm(opt.model, prompt, json_mode=True)
    parsed = extract_json(result.content)
    return convert_physical_index_to_int(parsed.get("physical_index", ""))


async def fix_incorrect_toc(
    toc, page_list, incorrect_results,
    *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1,
):
    incorrect_indices = {r["list_index"] for r in incorrect_results}
    end_index = len(page_list) + start_index - 1

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

        next_correct = end_index
        for j in range(list_index + 1, len(toc)):
            if j not in incorrect_indices and 0 <= j < len(toc):
                pi = toc[j].get("physical_index")
                if pi is not None:
                    next_correct = pi
                    break

        page_contents = []
        for page_index in range(prev_correct, next_correct + 1):
            li = page_index - start_index
            if 0 <= li < len(page_list):
                text = (
                    f"<physical_index_{page_index}>\n"
                    f"{page_list[li][0]}\n"
                    f"<physical_index_{page_index}>\n\n"
                )
                page_contents.append(text)

        content_range = "".join(page_contents)
        pi_int = await single_toc_item_index_fixer(
            incorrect_item["title"], content_range, opt=opt, call_llm=call_llm,
        )

        check_item = incorrect_item.copy()
        check_item["physical_index"] = pi_int
        check_result = await check_title_appearance(
            check_item, page_list, opt=opt, call_llm=call_llm, start_index=start_index,
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
    toc, page_list, incorrect_results,
    *, opt: BuildOpt, call_llm: CallLlm,
    start_index=1, max_attempts=3,
):
    current_toc = toc
    current_incorrect = incorrect_results

    for _ in range(max_attempts):
        if not current_incorrect:
            break
        current_toc, current_incorrect = await fix_incorrect_toc(
            current_toc, page_list, current_incorrect,
            opt=opt, call_llm=call_llm, start_index=start_index,
        )

    return current_toc, current_incorrect


# Main orchestration

async def meta_processor(
    page_list, *,
    opt: BuildOpt, call_llm: CallLlm, rng: random.Random,
    mode=None, toc_content=None, toc_page_list=None, start_index=1,
):
    if mode == "process_toc_with_page_numbers":
        toc = await process_toc_with_page_numbers(
            toc_content, toc_page_list, page_list,
            opt=opt, call_llm=call_llm,
            toc_check_page_num=opt.toc_check_page_num,
        )
    elif mode == "process_toc_no_page_numbers":
        toc = await process_toc_no_page_numbers(
            toc_content, toc_page_list, page_list,
            opt=opt, call_llm=call_llm,
        )
    else:
        toc = await process_no_toc(
            page_list, opt=opt, call_llm=call_llm,
            start_index=start_index, toc_hint=toc_content,
        )

    toc = [item for item in toc if item.get("physical_index") is not None]
    toc = validate_and_truncate_physical_indices(
        toc, len(page_list), start_index=start_index,
    )

    accuracy, incorrect = await verify_toc(
        page_list, toc,
        opt=opt, call_llm=call_llm, rng=rng, start_index=start_index,
    )

    log.debug("meta_processor mode=%s accuracy=%.2f incorrect=%d",
              mode, accuracy, len(incorrect))

    if accuracy == 1.0 and not incorrect:
        return toc
    if accuracy > 0.6 and incorrect:
        toc, _ = await fix_incorrect_toc_with_retries(
            toc, page_list, incorrect,
            opt=opt, call_llm=call_llm,
            start_index=start_index, max_attempts=3,
        )
        return toc

    # Fallback cascade
    if mode == "process_toc_with_page_numbers":
        return await meta_processor(
            page_list, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_no_page_numbers",
            toc_content=toc_content, toc_page_list=toc_page_list,
            start_index=start_index,
        )
    elif mode == "process_toc_no_page_numbers":
        return await meta_processor(
            page_list, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc",
            toc_content=toc_content, start_index=start_index,
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
    node, page_list, *,
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

    # Capture parent span before any mutation. start/end are section boundaries —
    # the overlap "cushion" used later by add_node_text() is not present here.
    parent_span = node["end_index"] - node["start_index"]

    node_pages = page_list[node["start_index"] - 1: node["end_index"]]
    token_num = sum(p[1] for p in node_pages)

    if (parent_span > opt.max_page_num_each_node
            and token_num >= opt.max_token_num_each_node):

        sub_toc = await meta_processor(
            node_pages, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc",
            start_index=node["start_index"],
        )
        sub_toc = await check_title_appearance_in_start_concurrent(
            sub_toc, page_list, opt=opt, call_llm=call_llm,
        )

        valid = [item for item in sub_toc if item.get("physical_index") is not None]

        if valid and node["title"].strip() == valid[0]["title"].strip():
            node["nodes"] = post_processing(valid[1:], node["end_index"])
            node["end_index"] = valid[1]["start_index"] if len(valid) > 1 else node["end_index"]
        else:
            node["nodes"] = post_processing(valid, node["end_index"])
            node["end_index"] = valid[0]["start_index"] if valid else node["end_index"]

        # Progress check: every child's page span must be strictly smaller than
        # the parent's original span. Stop recursing if the LLM produced a
        # degenerate split.
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
                child, page_list,
                opt=opt, call_llm=call_llm, rng=rng, _depth=_depth + 1,
            )
            for child in node["nodes"]
        ])

    return node


# Summarization & doc description

async def generate_node_summary(node, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt("generate_node_summary", opt.prompt_style, node_text=node["text"])
    result = await call_llm(opt.model_fast, prompt)
    return result.content


async def generate_summaries_for_structure(structure, *, opt: BuildOpt, call_llm: CallLlm):
    nodes = structure_to_list(structure)
    summaries = await asyncio.gather(*[
        generate_node_summary(node, opt=opt, call_llm=call_llm) for node in nodes
    ])
    for node, summary in zip(nodes, summaries):
        node["summary"] = summary
    return structure


async def verify_summaries_for_structure(
    structure, *, opt: BuildOpt, call_llm: CallLlm, max_retries: int = 1,
):
    """Check each node's summary against its section content; re-summarize once
    if topics are missed. Uses the fast model for verification."""
    nodes = structure_to_list(structure)

    async def verify_one(node):
        if not node.get("summary") or not node.get("text"):
            return
        section_text = _extract_section_content(node["text"])
        verify_prompt = get_prompt(
            "verify_node_summary", opt.prompt_style,
            title=node.get("title", ""),
            section_text=section_text,
            summary=node["summary"],
        )
        try:
            result = await call_llm(opt.model_fast, verify_prompt, json_mode=True)
            parsed = extract_json(result.content)
        except Exception as exc:
            log.warning(f"verify_node_summary failed for '{node.get('title')}': {exc}")
            return

        faithful = parsed.get("faithful", "no")
        missed = parsed.get("missed_topics") or []
        if faithful == "yes" and not missed:
            return
        if not missed:
            return

        for _ in range(max_retries):
            regen_prompt = get_prompt(
                "regenerate_summary_with_missed_topics", opt.prompt_style,
                node_text=node["text"],
                prior_summary=node["summary"],
                missed_topics=missed,
            )
            try:
                regen_result = await call_llm(opt.model_fast, regen_prompt)
                node["summary"] = regen_result.content
            except Exception as exc:
                log.warning(f"regenerate_summary failed for '{node.get('title')}': {exc}")
                return

    await asyncio.gather(*[verify_one(n) for n in nodes])
    return structure


async def generate_doc_description(structure, *, opt: BuildOpt, call_llm: CallLlm):
    prompt = get_prompt("generate_doc_description", opt.prompt_style, structure=structure)
    result = await call_llm(opt.model, prompt)
    return result.content


# Public entry

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
    pages: list[tuple[str, int]],
    opt: BuildOpt,
    *,
    call_llm: CallLlm,
    rng: random.Random,
    paper_id: str,
    pdf_path: str,
) -> DocumentTree:
    """Build a document tree from pre-loaded page-text + token-count tuples.

    Pure orchestration — all LLM calls go through `call_llm`, all randomness
    through `rng`. Safe to run inside the Temporal workflow sandbox.
    """
    total_tokens = sum(p[1] for p in pages)
    log.info("Pages: %d, tokens: %d", len(pages), total_tokens)

    check_result = await check_toc(pages, opt, call_llm=call_llm)
    log.debug("check_toc_result: %s", check_result)

    has_toc = (check_result.get("toc_content")
               and check_result["toc_content"].strip())

    if has_toc and check_result["page_index_given_in_toc"] == "yes":
        toc = await meta_processor(
            pages, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_with_page_numbers",
            start_index=1,
            toc_content=check_result["toc_content"],
            toc_page_list=check_result["toc_page_list"],
        )
    elif has_toc:
        toc = await meta_processor(
            pages, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_toc_no_page_numbers",
            start_index=1,
            toc_content=check_result["toc_content"],
            toc_page_list=check_result["toc_page_list"],
        )
    else:
        toc = await meta_processor(
            pages, opt=opt, call_llm=call_llm, rng=rng,
            mode="process_no_toc", start_index=1,
        )

    toc = add_preface_if_needed(toc)
    toc = await check_title_appearance_in_start_concurrent(
        toc, pages, opt=opt, call_llm=call_llm,
    )

    valid_toc = [item for item in toc if item.get("physical_index") is not None]
    tree_nodes = post_processing(valid_toc, len(pages))
    await asyncio.gather(*[
        process_large_node_recursively(
            node, pages, opt=opt, call_llm=call_llm, rng=rng,
        )
        for node in tree_nodes
    ])

    if opt.if_add_node_id == "yes":
        write_node_id(tree_nodes)

    if opt.if_add_node_summary == "yes":
        add_node_text(tree_nodes, pages, overlap_pages=opt.summary_overlap_pages)
        await generate_summaries_for_structure(tree_nodes, opt=opt, call_llm=call_llm)

        if opt.verify_summaries:
            await verify_summaries_for_structure(tree_nodes, opt=opt, call_llm=call_llm)

        remove_structure_text(tree_nodes)

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
        total_pages=len(pages),
        doc_description=doc_description,
        root_nodes=root_nodes,
    )
