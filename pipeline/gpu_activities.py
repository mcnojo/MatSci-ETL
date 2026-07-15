"""GPU-lane Temporal activities — HTTP clients to vLLM, no torch / no cv2.

Lives apart so the batch GPU ASG (GPU lane only, no workflow registration)
installs bare `pip install .` and skips the ~3-4 GB pipeline-cpu extra.
"""

import base64
import io
import json
import time
from collections import OrderedDict

import numpy as np
from openai import AsyncOpenAI
from temporalio import activity

from shared.prompts.etl import get_prompt
from shared.s3_io import get_bytes, put_bytes
from shared.temporal.activity_models import (
    ChandraCallInput,
    ChandraCallOutput,
    EmbedChunksInput,
    EmbedChunksOutput,
    LlmStructuredCallInput,
    LlmStructuredCallOutput,
    LlmTextCallInput,
    LlmTextCallOutput,
    PromptSpec,
)
from shared.vllm.resolve import resolve_vllm_url

from .heartbeat import await_with_heartbeats
from .llm_calls import execute_structured_call, execute_text_call
from .page_assembly import assemble_page_text
from .response_schemas import resolve_schema


# Bounded LRU — workers are long-lived and accumulate runs.
_CACHE_MAX = 8
_pages_cache: "OrderedDict[str, list[tuple[str, int]]]" = OrderedDict()
_config_cache: "OrderedDict[str, dict]" = OrderedDict()


def _cache_get_or_load(cache: OrderedDict, key: str, loader) -> object:
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = loader(key)
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)
    return value


def _load_pages(uri: str) -> list[tuple[str, int]]:
    raw = json.loads(get_bytes(uri).decode("utf-8"))
    return [(text, tokens) for text, tokens in raw]


def _load_config(uri: str) -> dict:
    return json.loads(get_bytes(uri).decode("utf-8"))


def _render_prompt(spec: PromptSpec) -> str:
    kwargs = dict(spec.small_kwargs)
    if spec.page_kwargs:
        if not spec.pages_uri:
            raise ValueError("PromptSpec.page_kwargs requires pages_uri to be set")
        pages = _cache_get_or_load(_pages_cache, spec.pages_uri, _load_pages)
        for name, page_spec in spec.page_kwargs.items():
            kwargs[name] = assemble_page_text(pages, page_spec)
    return get_prompt(spec.name, spec.style, **kwargs)


def _image_uri_to_b64(image_uri: str) -> str:
    return base64.b64encode(get_bytes(image_uri)).decode("utf-8")


def _openai_usage(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    if not u:
        return (0, 0)
    return (
        getattr(u, "prompt_tokens", 0) or 0,
        getattr(u, "completion_tokens", 0) or 0,
    )


@activity.defn(name="process-pdf_llm-text-call")
async def llm_text_call_activity(input: LlmTextCallInput) -> LlmTextCallOutput:
    activity.heartbeat()
    config = _cache_get_or_load(_config_cache, input.config_uri, _load_config)
    prompt = _render_prompt(input.prompt_spec)
    result = await await_with_heartbeats(
        execute_text_call(
            config, input.model, prompt,
            json_mode=input.json_mode,
        ),
    )
    return LlmTextCallOutput(
        model=result["model"],
        content=result["content"],
        finish_reason=result["finish_reason"],
        started_at=result["started_at"],
        ended_at=result["ended_at"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
    )


@activity.defn(name="process-pdf_llm-structured-call")
async def llm_structured_call_activity(
    input: LlmStructuredCallInput,
) -> LlmStructuredCallOutput:
    """instructor-backed variant of llm_text_call: returns a validated Pydantic
    model as a dict instead of raw content. The workflow selects the schema by
    registry key, so class objects don't cross the activity boundary.
    """
    activity.heartbeat()
    config = _cache_get_or_load(_config_cache, input.config_uri, _load_config)
    prompt = _render_prompt(input.prompt_spec)
    schema_cls = resolve_schema(input.response_schema)
    result = await await_with_heartbeats(
        execute_structured_call(
            config, input.model, prompt, schema_cls,
        ),
    )
    return LlmStructuredCallOutput(
        model=result["model"],
        data=result["data"],
        started_at=result["started_at"],
        ended_at=result["ended_at"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
    )


@activity.defn(name="process-pdf_chandra-vision-call")
async def chandra_vision_call_activity(input: ChandraCallInput) -> ChandraCallOutput:
    activity.heartbeat()
    # Resolve at boundary so OCR_VLLM_PREFER_PRIVATE_IP routes in-VPC over private IPs.
    base_url = resolve_vllm_url(input.base_url)
    b64 = _image_uri_to_b64(input.image_uri)
    client = AsyncOpenAI(base_url=base_url, api_key=input.api_key)
    started_at = time.time()
    response = await await_with_heartbeats(
        client.chat.completions.create(
            model=input.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": input.prompt},
                    ],
                },
            ],
            max_tokens=input.max_tokens,
            temperature=0.0,
        ),
    )
    ended_at = time.time()
    in_t, out_t = _openai_usage(response)
    return ChandraCallOutput(
        content=response.choices[0].message.content or "",
        started_at=started_at,
        ended_at=ended_at,
        input_tokens=in_t,
        output_tokens=out_t,
    )


@activity.defn(name="index-document_embed-chunks")
async def embed_chunks_activity(input: EmbedChunksInput) -> EmbedChunksOutput:
    """Read chunks JSON, batch-embed via the vLLM /embeddings endpoint, write
    embeddings as a fp16 .npy array. One-shot per document — batching inside.

    Positional alignment: embeddings[i] belongs to chunks[i]. The index
    activity assumes this and validates row-count parity.

    Format: numpy .npy, dtype float16, shape (N, dim). ~4-8× smaller than the
    JSON list-of-lists that preceded it (JSON floats are ~15 chars each);
    parse is ~10× faster on the read side. fp16 cosine drift on bge-m3 is
    empirically negligible (well under kNN retrieval noise), and OpenSearch
    upcasts to fp32 on ingest anyway.
    """
    activity.heartbeat()

    emb_cfg = input.config["embedding_server"]
    base_url = resolve_vllm_url(emb_cfg["base_url"])
    api_key = emb_cfg.get("api_key", "EMPTY")
    model = emb_cfg["model"]
    dim = int(emb_cfg["dimension"])
    batch_size = int(emb_cfg.get("batch_size", 64))

    raw = json.loads(get_bytes(input.chunks_uri).decode("utf-8"))
    texts = [c["text"] for c in raw]

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    started_at = time.time()

    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        activity.heartbeat()
        batch = texts[start:start + batch_size]
        resp = await await_with_heartbeats(
            client.embeddings.create(model=model, input=batch),
        )
        for row in resp.data:
            vec = row.embedding
            if len(vec) != dim:
                raise RuntimeError(
                    f"embedding_server.dimension={dim} but model returned "
                    f"{len(vec)}-dim vectors; rotate index_name and update config"
                )
            embeddings.append(list(vec))

    ended_at = time.time()

    # np.save writes the standard .npy header + raw dtype bytes; np.load on the
    # read side reconstructs shape + dtype without a separate schema. Empty
    # embedding lists produce a (0, dim) array — round-trip stays clean.
    arr = np.asarray(embeddings, dtype=np.float16)
    if arr.size == 0:
        arr = arr.reshape((0, dim))
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=False)
    put_bytes(
        input.embeddings_uri_out,
        buf.getvalue(),
        "application/octet-stream",
    )

    return EmbedChunksOutput(
        embeddings_uri=input.embeddings_uri_out,
        embedded_count=len(embeddings),
        dimension=dim,
        started_at=started_at,
        ended_at=ended_at,
    )


GPU_ACTIVITIES = [
    llm_text_call_activity,
    llm_structured_call_activity,
    chandra_vision_call_activity,
    embed_chunks_activity,
]
