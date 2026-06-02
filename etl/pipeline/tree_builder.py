"""
Tree builder — ported from VectifyAI/PageIndex (page_index.py + utils.py).

Builds a hierarchical document tree from a PDF by:
1. Extracting text per page via PyMuPDF
2. Detecting/extracting a table of contents (if present)
3. Mapping TOC entries to physical page indices
4. Falling back to LLM-generated structure when no TOC exists
5. Verifying and correcting page assignments via async LLM checks
6. Post-processing into a nested tree with start/end page ranges

Supports three LLM providers:
- ollama: local Ollama via OpenAI-compatible endpoint (default).
          Uses native Ollama JSON mode (`format: "json"`) and num_ctx for context.
- openai: any OpenAI-compatible endpoint (OpenAI, Together, OpenRouter, vLLM, MLX).
          Uses `response_format: {"type": "json_object"}` for JSON mode.
- anthropic: native Anthropic SDK. JSON enforced via prompt + validation retry
             (Anthropic has no native JSON mode for chat completions).

Provider is selected via tree_llm.provider in config. If omitted, auto-detected
from base_url (Ollama if :11434, else openai).

Prompt variants ("upstream" verbatim PageIndex vs "local" elaborated for small
models) are selected via tree_llm.prompt_style.
"""


import asyncio
import copy
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import tiktoken
from openai import AsyncOpenAI, OpenAI

from shared.schemas import DocumentTree, TreeNode

from .metrics import get_current_metrics
from .prompts import get_prompt

# Module-level state, set once by _init_clients
_provider: str = "ollama"          # "ollama" | "openai" | "anthropic"
_prompt_style: str = "local"       # "local" | "upstream"

# OpenAI-compatible (ollama / openai providers)
_sync_client: OpenAI | None = None
_async_client: AsyncOpenAI | None = None

# Anthropic native SDK
_anthropic_sync_client = None
_anthropic_async_client = None

_model_name: str = ""              # strong model for hard tasks
_model_name_fast: str = ""         # fast model (TOC detection, verification, summaries)
_num_ctx: int = 32768              # Ollama context window (input + output tokens)
_max_response_tokens: int = 8192   # cloud response cap (OpenAI max_tokens / Anthropic max_tokens)
_ollama_base: str = ""             # Ollama native API base (without /v1), empty if non-Ollama
_active_model: str | None = None   # currently loaded model in VRAM (Ollama only)

log = logging.getLogger("tree_builder")


def _init_clients(config: dict):
    """Initialize provider clients from tree_llm config. Called once per run."""
    global _provider, _prompt_style
    global _sync_client, _async_client, _anthropic_sync_client, _anthropic_async_client
    global _model_name, _model_name_fast
    global _num_ctx, _max_response_tokens, _ollama_base, _active_model

    cfg = config["tree_llm"]
    _model_name = cfg["model"]
    _model_name_fast = cfg.get("model_fast") or _model_name
    _prompt_style = (cfg.get("prompt_style") or "local").lower()
    if _prompt_style not in ("local", "upstream"):
        raise ValueError(
            f"tree_llm.prompt_style must be 'local' or 'upstream', got {_prompt_style!r}"
        )

    # Resolve provider: explicit > auto-detect from base_url
    explicit_provider = (cfg.get("provider") or "").lower() or None
    base_url = cfg.get("base_url") or "http://localhost:11434/v1"

    if explicit_provider:
        _provider = explicit_provider
    elif ":11434" in base_url and "/v1" in base_url:
        _provider = "ollama"
    else:
        _provider = "openai"

    if _provider not in ("ollama", "openai", "anthropic"):
        raise ValueError(
            f"tree_llm.provider must be 'ollama' | 'openai' | 'anthropic', got {_provider!r}"
        )

    # Context/response token budgets. `num_ctx` is Ollama-only; cloud providers
    # use `max_response_tokens` as the response cap. `max_tokens` is accepted
    # as a back-compat alias for num_ctx (the old single knob).
    _num_ctx = cfg.get("num_ctx") or cfg.get("max_tokens") or 32768
    _max_response_tokens = cfg.get("max_response_tokens", 8192)

    api_key_env = cfg.get("api_key_env")
    api_key = os.environ.get(api_key_env) if api_key_env else None

    _active_model = None
    _ollama_base = ""

    if _provider == "anthropic":
        try:
            from anthropic import Anthropic, AsyncAnthropic
        except ImportError as e:
            raise ImportError(
                "anthropic provider requires the `anthropic` package. "
                "Add it to requirements.txt and pip install."
            ) from e
        if not api_key:
            raise ValueError(
                f"anthropic provider requires api_key_env to point to an env var "
                f"holding the API key (got api_key_env={api_key_env!r})"
            )
        _anthropic_sync_client = Anthropic(api_key=api_key)
        _anthropic_async_client = AsyncAnthropic(api_key=api_key)
        # Anthropic doesn't use base_url; ignore it.
    else:
        # ollama or openai-compatible
        _sync_client = OpenAI(base_url=base_url, api_key=api_key or "local")
        _async_client = AsyncOpenAI(base_url=base_url, api_key=api_key or "local")
        if _provider == "ollama":
            # Strip /v1 suffix to get native Ollama API base for lifecycle ops
            if "/v1" in base_url:
                _ollama_base = base_url.rsplit("/v1", 1)[0]
            else:
                _ollama_base = base_url.rstrip("/")

    log.info(
        "Tree LLM: provider=%s, model=%s, model_fast=%s, prompt_style=%s, "
        "num_ctx=%d, max_response_tokens=%d",
        _provider, _model_name, _model_name_fast, _prompt_style,
        _num_ctx, _max_response_tokens,
    )


def _ensure_model_exclusive(model_name: str):
    """Unload the previously active model before loading a different one.

    On memory-constrained devices (e.g. M3 Pro 18GB), two models loaded
    simultaneously causes VRAM spill to CPU and inference timeouts.
    This ensures only one model occupies VRAM at a time.
    """
    global _active_model

    if not _ollama_base or model_name == _active_model:
        return

    if _active_model is not None:
        try:
            import httpx
            httpx.post(
                f"{_ollama_base}/api/generate",
                json={"model": _active_model, "keep_alive": 0},
                timeout=10,
            )
            log.info("Unloaded model %s from VRAM", _active_model)
        except Exception as e:
            log.debug("Failed to unload model %s: %s", _active_model, e)

    _active_model = model_name


# LLM call wrappers

_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model: str | None = None) -> int:
    if not text:
        return 0
    return len(_enc.encode(text))


def _looks_like_valid_json(content: str) -> bool:
    if not content or not content.strip():
        return False
    stripped = _strip_thinking(content).strip()
    if stripped.startswith("```"):
        stripped = _get_json_content(stripped)
    try:
        json.loads(stripped)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


# Provider-specific call shims

def _openai_messages_to_anthropic(messages: list) -> tuple[str | None, list]:
    """Split OpenAI-style messages into (system, messages) for Anthropic.

    Anthropic takes `system` as a top-level kwarg, not a message role.
    """
    system_parts = []
    anth_messages = []
    for m in messages:
        role = m["role"]
        if role == "system":
            system_parts.append(m["content"])
        else:
            anth_messages.append({"role": role, "content": m["content"]})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, anth_messages


def _anthropic_extract(response) -> tuple[str, str]:
    """Pull text content + normalized finish reason from an Anthropic response."""
    content = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            content += block.text
        elif hasattr(block, "text"):
            content += block.text
    finish = "length" if response.stop_reason == "max_tokens" else "stop"
    return content, finish


def _usage_openai(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0) if u else (0, 0)


def _usage_anthropic(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    return (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0) if u else (0, 0)


def _openai_chat_kwargs(model: str, messages: list, temperature: float, json_mode: bool) -> dict:
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}
    if _provider == "ollama":
        # Ollama: pass num_ctx + grammar-constrained JSON via extra_body
        extra_body: dict = {"num_ctx": _num_ctx}
        if json_mode:
            extra_body["format"] = "json"
        kwargs["extra_body"] = extra_body
    else:
        # Cloud OpenAI-compatible: response cap + standard JSON mode
        kwargs["max_tokens"] = _max_response_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def _call_sync(model: str, messages: list, temperature: float, json_mode: bool) -> tuple[str, str]:
    t0 = time.perf_counter()
    errored = False
    usage = (0, 0)
    try:
        if _provider == "anthropic":
            system, anth_messages = _openai_messages_to_anthropic(messages)
            kwargs = {
                "model": model,
                "max_tokens": _max_response_tokens,
                "temperature": temperature,
                "messages": anth_messages,
            }
            if system:
                kwargs["system"] = system
            response = _anthropic_sync_client.messages.create(**kwargs)
            usage = _usage_anthropic(response)
            return _anthropic_extract(response)

        kwargs = _openai_chat_kwargs(model, messages, temperature, json_mode)
        response = _sync_client.chat.completions.create(**kwargs)
        usage = _usage_openai(response)
        content = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason
        return content, finish
    except Exception:
        errored = True
        raise
    finally:
        m = get_current_metrics()
        if m is not None:
            m.record_llm_call(
                model, time.perf_counter() - t0, error=errored,
                input_tokens=usage[0], output_tokens=usage[1],
            )


async def _call_async(model: str, messages: list, temperature: float, json_mode: bool) -> tuple[str, str]:
    t0 = time.perf_counter()
    errored = False
    usage = (0, 0)
    try:
        if _provider == "anthropic":
            system, anth_messages = _openai_messages_to_anthropic(messages)
            kwargs = {
                "model": model,
                "max_tokens": _max_response_tokens,
                "temperature": temperature,
                "messages": anth_messages,
            }
            if system:
                kwargs["system"] = system
            response = await _anthropic_async_client.messages.create(**kwargs)
            usage = _usage_anthropic(response)
            return _anthropic_extract(response)

        kwargs = _openai_chat_kwargs(model, messages, temperature, json_mode)
        response = await _async_client.chat.completions.create(**kwargs)
        usage = _usage_openai(response)
        content = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason
        return content, finish
    except Exception:
        errored = True
        raise
    finally:
        m = get_current_metrics()
        if m is not None:
            m.record_llm_call(
                model, time.perf_counter() - t0, error=errored,
                input_tokens=usage[0], output_tokens=usage[1],
            )


def llm_completion(
    model: str,
    prompt: str,
    chat_history: list | None = None,
    return_finish_reason: bool = False,
    json_mode: bool = False,
) -> str | tuple[str, str]:
    """model arg is kept for call-site compat but the actual model is resolved here."""
    use_model = model if model and model != _model_name else _model_name
    _ensure_model_exclusive(use_model)
    max_retries = 5
    messages = (list(chat_history) + [{"role": "user", "content": prompt}]
                if chat_history else [{"role": "user", "content": prompt}])

    log.debug("llm_completion [%s]: prompt length=%d chars", use_model, len(prompt))

    for i in range(max_retries):
        try:
            # Bump temperature on retry to escape deterministic bad outputs
            temperature = 0.0 if i == 0 else min(0.6, 0.2 * i)
            content, finish = _call_sync(use_model, messages, temperature, json_mode)

            log.debug(
                "llm_completion [%s]: response length=%d chars, finish_reason=%s, first 200: %.200s",
                use_model, len(content), finish, content or "(empty)",
            )

            if json_mode and finish != "length" and not _looks_like_valid_json(content):
                log.warning(
                    "llm_completion: json_mode response did not parse (attempt %d/%d); retrying",
                    i + 1, max_retries,
                )
                continue

            if return_finish_reason:
                reason = ("max_output_reached" if finish == "length" else "finished")
                return content, reason
            return content
        except Exception as e:
            log.error("LLM call failed (attempt %d/%d): %s", i + 1, max_retries, e)
            if i < max_retries - 1:
                time.sleep(2)
            else:
                if return_finish_reason:
                    return "", "error"
                return ""

    if return_finish_reason:
        return "", "error"
    return ""


async def llm_acompletion(model: str, prompt: str, json_mode: bool = False) -> str:
    use_model = model if model and model != _model_name else _model_name
    _ensure_model_exclusive(use_model)
    max_retries = 5
    messages = [{"role": "user", "content": prompt}]

    log.debug("llm_acompletion [%s]: prompt length=%d chars", use_model, len(prompt))

    for i in range(max_retries):
        try:
            temperature = 0.0 if i == 0 else min(0.6, 0.2 * i)
            content, finish = await _call_async(use_model, messages, temperature, json_mode)

            log.debug(
                "llm_acompletion [%s]: response length=%d chars, finish_reason=%s, first 200: %.200s",
                use_model, len(content), finish, content or "(empty)",
            )

            if json_mode and finish != "length" and not _looks_like_valid_json(content):
                log.warning(
                    "llm_acompletion: json_mode response did not parse (attempt %d/%d); retrying",
                    i + 1, max_retries,
                )
                continue

            return content
        except Exception as e:
            log.error("Async LLM call failed (attempt %d/%d): %s", i + 1, max_retries, e)
            if i < max_retries - 1:
                await asyncio.sleep(2)
            else:
                return ""

    return ""


# JSON extraction

def _get_json_content(response: str) -> str:
    start_idx = response.find("```json")
    if start_idx != -1:
        response = response[start_idx + 7:]
    end_idx = response.rfind("```")
    if end_idx != -1:
        response = response[:end_idx]
    return response.strip()


def _strip_thinking(content: str) -> str:
    """Remove <think>...</think> blocks that reasoning models (Qwen3.5, DeepSeek) emit."""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def extract_json(content: str) -> dict | list:
    if not content or not content.strip():
        logging.error("extract_json: received empty response from LLM")
        return {}

    # Strip thinking blocks from reasoning models
    content = _strip_thinking(content)

    if not content.strip():
        logging.error("extract_json: response was only thinking tokens, no content")
        return {}

    try:
        # Strip markdown fences
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
        # Try fixing trailing commas
        try:
            json_content = json_content.replace(",]", "]").replace(",}", "}")
            return json.loads(json_content)
        except Exception:
            pass

        # Last resort: find the outermost JSON object or array
        try:
            match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", content)
            if match:
                return json.loads(match.group(1))
        except Exception:
            pass

        logging.error(
            "extract_json: failed to parse. First 500 chars of response:\n%s",
            content[:500],
        )
        return {}
    except Exception as e:
        logging.error("extract_json: unexpected error: %s", e)
        return {}


# PDF text extraction

def get_page_tokens(pdf_path: str, model: str | None = None) -> list[tuple[str, int]]:
    doc = pymupdf.open(pdf_path)
    page_list = []
    for page in doc:
        text = page.get_text()
        tokens = count_tokens(text, model)
        page_list.append((text, tokens))
    doc.close()
    return page_list


def is_likely_scanned(page_list: list[tuple[str, int]], threshold: int = 30) -> bool:
    word_counts = [len(text.split()) for text, _ in page_list]
    if not word_counts:
        return True
    median_words = sorted(word_counts)[len(word_counts) // 2]
    return median_words < threshold


# Utility functions (from PageIndex utils.py)

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


def validate_and_truncate_physical_indices(
    toc, page_list_length, start_index=1,
):
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


def page_list_to_group_text(
    page_contents, token_lengths, max_tokens=10000, overlap_page=1
):
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


# TOC detection & extraction

def toc_detector_single_page(content, model=None):
    model = _model_name_fast  # simple yes/no — always use fast model
    prompt = get_prompt("toc_detector_single_page", _prompt_style, content=content)
    response = llm_completion(model=model, prompt=prompt, json_mode=True)
    return extract_json(response).get("toc_detected", "no")


def find_toc_pages(start_page_index, page_list, opt):
    last_page_is_yes = False
    toc_page_list = []
    i = start_page_index

    while i < len(page_list):
        if i >= opt.toc_check_page_num and not last_page_is_yes:
            break
        result = toc_detector_single_page(page_list[i][0], model=opt.model)
        if result == "yes":
            toc_page_list.append(i)
            last_page_is_yes = True
        elif result == "no" and last_page_is_yes:
            break
        i += 1

    return toc_page_list


def detect_page_index(toc_content, model=None):
    model = _model_name_fast  # simple yes/no — always use fast model
    prompt = get_prompt("detect_page_index", _prompt_style, toc_content=toc_content)
    response = llm_completion(model=model, prompt=prompt, json_mode=True)
    return extract_json(response).get("page_index_given_in_toc", "no")


def toc_extractor(page_list, toc_page_list, model):
    def transform_dots(text):
        text = re.sub(r"\.{5,}", ": ", text)
        text = re.sub(r"(?:\. ){5,}\.?", ": ", text)
        return text

    toc_content = ""
    for idx in toc_page_list:
        toc_content += page_list[idx][0]
    toc_content = transform_dots(toc_content)
    has_page_index = detect_page_index(toc_content, model=model)
    return {"toc_content": toc_content, "page_index_given_in_toc": has_page_index}


def check_if_toc_transformation_is_complete(content, toc, model=None):
    model = _model_name_fast  # simple yes/no — always use fast model
    prompt = get_prompt(
        "check_if_toc_transformation_is_complete", _prompt_style,
        content=content, toc=toc,
    )
    response = llm_completion(model=model, prompt=prompt, json_mode=True)
    return extract_json(response).get("completed", "no")


def extract_toc_content(content, model=None):
    prompt = get_prompt("extract_toc_content", _prompt_style, content=content)
    response, finish_reason = llm_completion(
        model=model, prompt=prompt, return_finish_reason=True
    )
    if_complete = check_if_toc_transformation_is_complete(content, response, model)
    if if_complete == "yes" and finish_reason == "finished":
        return response

    chat_history = [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": response},
    ]
    cont_prompt = get_prompt("extract_toc_content_continue", _prompt_style)

    for _ in range(5):
        new_response, finish_reason = llm_completion(
            model=model, prompt=cont_prompt,
            chat_history=chat_history, return_finish_reason=True,
        )
        response += new_response
        if_complete = check_if_toc_transformation_is_complete(content, response, model)
        if if_complete == "yes" and finish_reason == "finished":
            return response
        chat_history = [
            {"role": "user", "content": cont_prompt},
            {"role": "assistant", "content": response},
        ]

    raise RuntimeError("Failed to complete table of contents after maximum retries")


def _extract_toc_list(parsed: dict | list) -> list | None:
    """Pull the TOC list from whatever shape the LLM returned.

    Expected: {"table_of_contents": [...]}, but models sometimes return
    just the list directly, or use variant keys.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("table_of_contents", "toc", "contents"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        # Single-key dict wrapping a list
        if len(parsed) == 1:
            val = next(iter(parsed.values()))
            if isinstance(val, list):
                return val
    return None


def toc_transformer(toc_content, model=None):
    prompt = get_prompt("toc_transformer", _prompt_style, toc_content=toc_content)
    last_complete, finish_reason = llm_completion(
        model=model, prompt=prompt, return_finish_reason=True, json_mode=True
    )
    if_complete = check_if_toc_transformation_is_complete(toc_content, last_complete, model)
    if if_complete == "yes" and finish_reason == "finished":
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
            "toc_transformer_continue", _prompt_style,
            toc_content=toc_content, last_complete=last_complete,
        )

        new_complete, finish_reason = llm_completion(
            model=model, prompt=cont_prompt, return_finish_reason=True, json_mode=True
        )
        if new_complete.startswith("```json"):
            new_complete = _get_json_content(new_complete)
            last_complete += new_complete

        if_complete = check_if_toc_transformation_is_complete(
            toc_content, last_complete, model
        )
        if if_complete == "yes" and finish_reason == "finished":
            break

    parsed = extract_json(last_complete)
    toc_list = _extract_toc_list(parsed)
    if toc_list is None:
        log.error("toc_transformer: could not extract TOC list from LLM response")
        return []
    return convert_page_to_int(toc_list)


def toc_index_extractor(toc, content, model=None):
    prompt = get_prompt("toc_index_extractor", _prompt_style, toc=toc, content=content)
    response = llm_completion(model=model, prompt=prompt, json_mode=True)
    return extract_json(response)


# Page number assignment

def add_page_number_to_toc(part, structure, model=None):
    prompt = get_prompt(
        "add_page_number_to_toc", _prompt_style,
        part=part, structure=structure,
    )
    response = llm_completion(model=model, prompt=prompt, json_mode=True)
    result = extract_json(response)

    for item in result:
        item.pop("start", None)
    return result


# No-TOC tree generation

def generate_toc_init(part, model=None, toc_hint=None):
    if toc_hint:
        prompt = get_prompt(
            "generate_toc_init_with_hint", _prompt_style,
            part=part, toc_hint=toc_hint,
        )
    else:
        prompt = get_prompt("generate_toc_init", _prompt_style, part=part)
    response, finish_reason = llm_completion(
        model=model, prompt=prompt, return_finish_reason=True, json_mode=True
    )
    if finish_reason == "finished":
        return extract_json(response)
    raise RuntimeError(f"generate_toc_init: finish_reason={finish_reason}")


def generate_toc_continue(toc_content, part, model=None):
    prompt = get_prompt(
        "generate_toc_continue", _prompt_style,
        toc_content=toc_content, part=part,
    )
    response, finish_reason = llm_completion(
        model=model, prompt=prompt, return_finish_reason=True, json_mode=True
    )
    if finish_reason == "finished":
        return extract_json(response)
    raise RuntimeError(f"generate_toc_continue: finish_reason={finish_reason}")


# TOC processing paths

def process_no_toc(page_list, start_index=1, model=None, toc_hint=None):
    """Extract section structure from page text via chunked LLM passes.

    If `toc_hint` is provided (a TOC found on a page in the document),
    it's passed to the first chunk as a non-authoritative guide.
    """
    page_contents = []
    token_lengths = []
    for page_index in range(start_index, start_index + len(page_list)):
        text = (
            f"<physical_index_{page_index}>\n"
            f"{page_list[page_index - start_index][0]}\n"
            f"<physical_index_{page_index}>\n\n"
        )
        page_contents.append(text)
        token_lengths.append(count_tokens(text, model))

    group_texts = page_list_to_group_text(page_contents, token_lengths)
    log.debug("process_no_toc: %d group(s)%s", len(group_texts),
              f" with TOC hint ({len(toc_hint)} chars)" if toc_hint else "")

    toc = generate_toc_init(group_texts[0], model, toc_hint=toc_hint)
    for group_text in group_texts[1:]:
        additional = generate_toc_continue(toc, group_text, model)
        toc.extend(additional)

    log.debug("generate_toc result: %s", toc)

    return convert_physical_index_to_int(toc)


def process_toc_no_page_numbers(
    toc_content, toc_page_list, page_list, start_index=1, model=None,
):
    toc_structured = toc_transformer(toc_content, model)
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
        token_lengths.append(count_tokens(text, model))

    group_texts = page_list_to_group_text(page_contents, token_lengths)
    log.debug("process_toc_no_page_numbers: %d group(s)", len(group_texts))

    toc_with_pages = copy.deepcopy(toc_structured)
    for group_text in group_texts:
        toc_with_pages = add_page_number_to_toc(group_text, toc_with_pages, model)

    log.debug("add_page_number_to_toc: %s", toc_with_pages)

    return convert_physical_index_to_int(toc_with_pages)


def process_none_page_numbers(toc_items, page_list, start_index=1, model=None):
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
            result = add_page_number_to_toc(page_contents, item_copy, model)
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


def process_toc_with_page_numbers(
    toc_content, toc_page_list, page_list,
    toc_check_page_num=None, model=None,
):
    toc_with_pages = toc_transformer(toc_content, model)
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

    toc_with_physical = toc_index_extractor(toc_no_pages, main_content, model)
    log.debug("toc_with_physical_index: %s", toc_with_physical)

    toc_with_physical = convert_physical_index_to_int(toc_with_physical)

    pairs = extract_matching_page_pairs(toc_with_pages, toc_with_physical, start_page)
    log.debug("matching_pairs: %s", pairs)

    offset = calculate_page_offset(pairs)
    log.debug("offset: %s", offset)

    toc_with_pages = add_page_offset_to_toc_json(toc_with_pages, offset)
    toc_with_pages = process_none_page_numbers(toc_with_pages, page_list, model=model)

    log.debug("final toc_with_page_number: %s", toc_with_pages)

    return toc_with_pages


def check_toc(page_list, opt):
    # Cap TOC search to the first third of the document — a TOC deeper than
    # that is effectively absent for structural purposes, and scanning further
    # just increases false-positive risk on scientific papers.
    opt = copy.copy(opt)
    opt.toc_check_page_num = min(opt.toc_check_page_num, max(3, len(page_list) // 3))

    toc_page_list = find_toc_pages(start_page_index=0, page_list=page_list, opt=opt)
    if not toc_page_list:
        return {"toc_content": None, "toc_page_list": [], "page_index_given_in_toc": "no"}

    toc_json = toc_extractor(page_list, toc_page_list, opt.model)
    if toc_json["page_index_given_in_toc"] == "yes":
        return {
            "toc_content": toc_json["toc_content"],
            "toc_page_list": toc_page_list,
            "page_index_given_in_toc": "yes",
        }

    current_start = toc_page_list[-1] + 1
    while (toc_json["page_index_given_in_toc"] == "no"
           and current_start < len(page_list)
           and current_start < opt.toc_check_page_num):
        additional = find_toc_pages(
            start_page_index=current_start, page_list=page_list, opt=opt
        )
        if not additional:
            break
        additional_json = toc_extractor(page_list, additional, opt.model)
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

async def check_title_appearance(item, page_list, start_index=1, model=None):
    model = _model_name_fast  # simple yes/no — always use fast model
    title = item["title"]
    if "physical_index" not in item or item["physical_index"] is None:
        return {"list_index": item.get("list_index"), "answer": "no",
                "title": title, "page_number": None}

    page_number = item["physical_index"]
    page_text = page_list[page_number - start_index][0]

    prompt = get_prompt(
        "check_title_appearance", _prompt_style,
        title=title, page_text=page_text,
    )
    response = await llm_acompletion(model=model, prompt=prompt, json_mode=True)
    parsed = extract_json(response)
    answer = parsed.get("answer", "no")
    return {"list_index": item.get("list_index"), "answer": answer,
            "title": title, "page_number": page_number}


async def check_title_appearance_in_start(title, page_text, model=None):
    model = _model_name_fast  # simple yes/no — always use fast model
    prompt = get_prompt(
        "check_title_appearance_in_start", _prompt_style,
        title=title, page_text=page_text,
    )
    response = await llm_acompletion(model=model, prompt=prompt, json_mode=True)
    return extract_json(response).get("start_begin", "no")


async def check_title_appearance_in_start_concurrent(structure, page_list, model=None):
    for item in structure:
        if item.get("physical_index") is None:
            item["appear_start"] = "no"

    tasks = []
    valid_items = []
    for item in structure:
        if item.get("physical_index") is not None:
            page_text = page_list[item["physical_index"] - 1][0]
            tasks.append(
                check_title_appearance_in_start(item["title"], page_text, model=model)
            )
            valid_items.append(item)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item, result in zip(valid_items, results):
        if isinstance(result, Exception):
            item["appear_start"] = "no"
        else:
            item["appear_start"] = result

    return structure


async def verify_toc(page_list, list_result, start_index=1, N=None, model=None):
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
        sample_indices = random.sample(range(len(list_result)), N)

    indexed = []
    for idx in sample_indices:
        item = list_result[idx]
        if item.get("physical_index") is not None:
            item_copy = item.copy()
            item_copy["list_index"] = idx
            indexed.append(item_copy)

    results = await asyncio.gather(
        *[check_title_appearance(item, page_list, start_index, model) for item in indexed]
    )

    correct = sum(1 for r in results if r["answer"] == "yes")
    incorrect = [r for r in results if r["answer"] != "yes"]
    accuracy = correct / len(results) if results else 0
    return accuracy, incorrect


async def single_toc_item_index_fixer(section_title, content, model=None):
    prompt = get_prompt(
        "single_toc_item_index_fixer", _prompt_style,
        section_title=section_title, content=content,
    )
    response = await llm_acompletion(model=model, prompt=prompt, json_mode=True)
    parsed = extract_json(response)
    return convert_physical_index_to_int(parsed.get("physical_index", ""))


async def fix_incorrect_toc(
    toc, page_list, incorrect_results, start_index=1, model=None,
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
            incorrect_item["title"], content_range, model
        )

        check_item = incorrect_item.copy()
        check_item["physical_index"] = pi_int
        check_result = await check_title_appearance(check_item, page_list, start_index, model)

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
    start_index=1, max_attempts=3, model=None,
):
    current_toc = toc
    current_incorrect = incorrect_results

    for attempt in range(max_attempts):
        if not current_incorrect:
            break
        current_toc, current_incorrect = await fix_incorrect_toc(
            current_toc, page_list, current_incorrect, start_index, model,
        )

    return current_toc, current_incorrect


# Main orchestration

async def meta_processor(
    page_list, mode=None, toc_content=None, toc_page_list=None,
    start_index=1, opt=None,
):
    if mode == "process_toc_with_page_numbers":
        toc = process_toc_with_page_numbers(
            toc_content, toc_page_list, page_list,
            toc_check_page_num=opt.toc_check_page_num, model=opt.model,
        )
    elif mode == "process_toc_no_page_numbers":
        toc = process_toc_no_page_numbers(
            toc_content, toc_page_list, page_list,
            model=opt.model,
        )
    else:
        toc = process_no_toc(
            page_list, start_index=start_index, model=opt.model,
            toc_hint=toc_content,
        )

    toc = [item for item in toc if item.get("physical_index") is not None]
    toc = validate_and_truncate_physical_indices(
        toc, len(page_list), start_index=start_index,
    )

    accuracy, incorrect = await verify_toc(
        page_list, toc, start_index=start_index, model=opt.model,
    )

    log.debug("meta_processor mode=%s accuracy=%.2f incorrect=%d", mode, accuracy, len(incorrect))

    if accuracy == 1.0 and not incorrect:
        return toc
    if accuracy > 0.6 and incorrect:
        toc, _ = await fix_incorrect_toc_with_retries(
            toc, page_list, incorrect,
            start_index=start_index, max_attempts=3, model=opt.model,
        )
        return toc

    # Fallback cascade
    if mode == "process_toc_with_page_numbers":
        return await meta_processor(
            page_list, mode="process_toc_no_page_numbers",
            toc_content=toc_content, toc_page_list=toc_page_list,
            start_index=start_index, opt=opt,
        )
    elif mode == "process_toc_no_page_numbers":
        return await meta_processor(
            page_list, mode="process_no_toc",
            toc_content=toc_content,
            start_index=start_index, opt=opt,
        )
    else:
        # Fail gracefully 
        log.warning(
            "Tree building: all processing modes failed verification "
            "(accuracy=%.2f). Returning best-effort TOC (%d items) — "
            "structure may be incomplete.",
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


async def process_large_node_recursively(node, page_list, opt=None, _depth=0):
    if _depth >= _MAX_SUBDIVISION_DEPTH:
        log.warning(
            "Max subdivision depth (%d) reached for node '%s' (pages %d–%d). "
            "Stopping further splitting.",
            _MAX_SUBDIVISION_DEPTH, node.get("title", "?"),
            node["start_index"], node["end_index"],
        )
        return node

    # Capture the parent's structural page span BEFORE any mutation.
    # start_index/end_index are section boundaries — the overlap "cushion"
    # pages used later by add_node_text() for summarization context are not
    # present here, so this comparison is clean.
    parent_span = node["end_index"] - node["start_index"]

    node_pages = page_list[node["start_index"] - 1: node["end_index"]]
    token_num = sum(p[1] for p in node_pages)

    if (parent_span > opt.max_page_num_each_node
            and token_num >= opt.max_token_num_each_node):

        sub_toc = await meta_processor(
            node_pages, mode="process_no_toc",
            start_index=node["start_index"], opt=opt,
        )
        sub_toc = await check_title_appearance_in_start_concurrent(
            sub_toc, page_list, model=opt.model,
        )

        valid = [item for item in sub_toc if item.get("physical_index") is not None]

        if valid and node["title"].strip() == valid[0]["title"].strip():
            node["nodes"] = post_processing(valid[1:], node["end_index"])
            node["end_index"] = valid[1]["start_index"] if len(valid) > 1 else node["end_index"]
        else:
            node["nodes"] = post_processing(valid, node["end_index"])
            node["end_index"] = valid[0]["start_index"] if valid else node["end_index"]

        # Progress check: every child's page span must be strictly smaller
        # than the parent's original span. If the LLM produced a degenerate
        # split (e.g. a single child covering the same range), stop here
        # rather than recursing forever.
        if node.get("nodes"):
            stalled = [
                c for c in node["nodes"]
                if c["end_index"] - c["start_index"] >= parent_span
            ]
            if stalled:
                log.warning(
                    "Subdivision made no progress for node '%s' (pages %d–%d): "
                    "%d child(ren) have span >= parent. Keeping children as leaves.",
                    node.get("title", "?"), node["start_index"], node["end_index"],
                    len(stalled),
                )
                return node

    if node.get("nodes"):
        await asyncio.gather(
            *[process_large_node_recursively(child, page_list, opt, _depth=_depth + 1)
              for child in node["nodes"]]
        )

    return node


async def generate_node_summary(node, model=None):
    model = _model_name_fast  # straightforward summarization — always use fast model
    prompt = get_prompt("generate_node_summary", _prompt_style, node_text=node["text"])
    return await llm_acompletion(model, prompt)


async def generate_summaries_for_structure(structure, model=None):
    nodes = structure_to_list(structure)
    summaries = await asyncio.gather(
        *[generate_node_summary(node, model=model) for node in nodes]
    )
    for node, summary in zip(nodes, summaries):
        node["summary"] = summary
    return structure


def _extract_section_content(text: str) -> str:
    """Pull just the <<<section-content>>> block out of overlap-wrapped text.
    Returns the text unchanged if no marker is present."""
    start_marker = "<<<section-content>>>"
    end_marker = "<<<context-after>>>"
    si = text.find(start_marker)
    if si == -1:
        return text
    si += len(start_marker)
    ei = text.find(end_marker, si)
    return text[si:ei].strip() if ei != -1 else text[si:].strip()


async def verify_summaries_for_structure(structure, model=None, max_retries: int = 1):
    """Check each node's summary against its section content; re-summarize once
    if topics are missed. Uses the fast model for verification."""
    nodes = structure_to_list(structure)

    async def verify_one(node):
        if not node.get("summary") or not node.get("text"):
            return
        section_text = _extract_section_content(node["text"])
        verify_prompt = get_prompt(
            "verify_node_summary", _prompt_style,
            title=node.get("title", ""),
            section_text=section_text,
            summary=node["summary"],
        )
        try:
            response = await llm_acompletion(
                model=_model_name_fast, prompt=verify_prompt, json_mode=True,
            )
            parsed = extract_json(response)
        except Exception as exc:
            log.warning(f"verify_node_summary failed for '{node.get('title')}': {exc}")
            return

        faithful = parsed.get("faithful", "no")
        missed = parsed.get("missed_topics") or []
        if faithful == "yes" and not missed:
            return
        if not missed:
            return  # nothing actionable

        for _ in range(max_retries):
            regen_prompt = get_prompt(
                "regenerate_summary_with_missed_topics", _prompt_style,
                node_text=node["text"],
                prior_summary=node["summary"],
                missed_topics=missed,
            )
            try:
                node["summary"] = await llm_acompletion(
                    model=_model_name_fast, prompt=regen_prompt,
                )
            except Exception as exc:
                log.warning(f"regenerate_summary failed for '{node.get('title')}': {exc}")
                return

    await asyncio.gather(*[verify_one(n) for n in nodes])
    return structure


def generate_doc_description(structure, model=None):
    prompt = get_prompt("generate_doc_description", _prompt_style, structure=structure)
    return llm_completion(model, prompt)


# Public entry points

async def build_tree_async(
    pdf_path: str, config: dict,
) -> DocumentTree:
    """Build a document tree from a PDF. Returns a DocumentTree (no enrichment).

    LLM call metrics are recorded into the current PipelineMetrics (if set
    via set_current_metrics before calling this function).
    """
    metrics = get_current_metrics()
    tree_cfg = config["tree"]
    model = config["tree_llm"]["model"]

    opt = SimpleNamespace(
        model=model,
        toc_check_page_num=tree_cfg["toc_check_pages"],
        max_page_num_each_node=tree_cfg["max_pages_per_node"],
        max_token_num_each_node=tree_cfg["max_tokens_per_node"],
        if_add_node_id="yes" if tree_cfg.get("add_node_id", True) else "no",
        if_add_node_summary="yes" if tree_cfg.get("add_node_summary", True) else "no",
        if_add_doc_description="yes" if tree_cfg.get("add_doc_description", False) else "no",
        if_add_node_text="no",
    )

    _init_clients(config)

    paper_id = Path(pdf_path).stem
    page_list = get_page_tokens(pdf_path, model=model)

    if is_likely_scanned(page_list):
        log.warning(
            "PDF appears to be scanned (low extractable text). "
            "Tree quality may be poor — consider OCR preprocessing."
        )

    total_tokens = sum(p[1] for p in page_list)
    log.info("Pages: %d, tokens: %d", len(page_list), total_tokens)
    if metrics:
        metrics.record_counts(total_pages=len(page_list))

    # 1. Check for TOC
    check_toc_result = check_toc(page_list, opt)
    log.debug("check_toc_result: %s", check_toc_result)

    has_toc = (check_toc_result.get("toc_content")
               and check_toc_result["toc_content"].strip())

    if has_toc and check_toc_result["page_index_given_in_toc"] == "yes":
        toc = await meta_processor(
            page_list, mode="process_toc_with_page_numbers",
            start_index=1,
            toc_content=check_toc_result["toc_content"],
            toc_page_list=check_toc_result["toc_page_list"],
            opt=opt,
        )
    elif has_toc:
        toc = await meta_processor(
            page_list, mode="process_toc_no_page_numbers",
            start_index=1,
            toc_content=check_toc_result["toc_content"],
            toc_page_list=check_toc_result["toc_page_list"],
            opt=opt,
        )
    else:
        toc = await meta_processor(
            page_list, mode="process_no_toc",
            start_index=1, opt=opt,
        )

    toc = add_preface_if_needed(toc)
    toc = await check_title_appearance_in_start_concurrent(
        toc, page_list, model=opt.model,
    )

    valid_toc = [item for item in toc if item.get("physical_index") is not None]
    tree_nodes = post_processing(valid_toc, len(page_list))
    await asyncio.gather(
        *[process_large_node_recursively(node, page_list, opt) for node in tree_nodes]
    )

    if opt.if_add_node_id == "yes":
        write_node_id(tree_nodes)

    if opt.if_add_node_summary == "yes":
        overlap_pages = tree_cfg.get("summary_overlap_pages", 1)
        add_node_text(tree_nodes, page_list, overlap_pages=overlap_pages)
        await generate_summaries_for_structure(tree_nodes, model=model)

        if tree_cfg.get("verify_summaries", True):
            await verify_summaries_for_structure(tree_nodes, model=model)

        remove_structure_text(tree_nodes)

    doc_description = None
    if opt.if_add_doc_description == "yes":
        clean = create_clean_structure_for_description(tree_nodes)
        doc_description = generate_doc_description(clean, model=model)

    tree_nodes = format_structure(
        tree_nodes,
        order=["title", "node_id", "start_index", "end_index", "summary", "nodes"],
    )

    root_nodes = _dicts_to_tree_nodes(tree_nodes)

    node_count = sum(1 for _ in _flatten_nodes(root_nodes))
    if metrics:
        metrics.record_counts(node_count=node_count)

    return DocumentTree(
        paper_id=paper_id,
        pdf_path=str(Path(pdf_path).resolve()),
        total_pages=len(page_list),
        doc_description=doc_description,
        root_nodes=root_nodes,
    )


def _flatten_nodes(nodes):
    for n in nodes:
        yield n
        yield from _flatten_nodes(n.nodes)


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


def build_tree(pdf_path: str, config: dict) -> DocumentTree:
    """Synchronous wrapper around build_tree_async."""
    return asyncio.run(build_tree_async(pdf_path, config))
