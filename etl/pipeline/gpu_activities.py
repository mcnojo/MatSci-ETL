"""GPU-lane Temporal activities — HTTP clients to vLLM, no torch / no cv2.

Lives apart so the batch GPU ASG (GPU lane only, no workflow registration)
installs bare `pip install .` and skips the ~3-4 GB pipeline-cpu extra.
"""

import base64
import json
import time
from collections import OrderedDict

from openai import AsyncOpenAI
from temporalio import activity

from shared.prompts.etl import get_prompt
from shared.s3_io import get_bytes
from shared.temporal.activity_models import (
    ChandraCallInput,
    ChandraCallOutput,
    LlmTextCallInput,
    LlmTextCallOutput,
    PromptSpec,
)
from shared.vllm.resolve import resolve_vllm_url

from .heartbeat import await_with_heartbeats
from .llm_calls import execute_text_call
from .page_assembly import assemble_page_text


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
            json_mode=input.json_mode, temperature=input.temperature,
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


GPU_ACTIVITIES = [
    llm_text_call_activity,
    chandra_vision_call_activity,
]
