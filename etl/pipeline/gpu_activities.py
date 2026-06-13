"""GPU-lane Temporal activities.

Per-call LLM/vision HTTP activities — `llm_text_call` and `chandra_vision_call`.
Both are HTTP clients to vLLM; no local model inference, no torch, no cv2.
Kept in their own module so the batch GPU ASG (which only polls the GPU lane
and registers no workflows) can install bare `pip install .` and skip the
~3-4 GB pipeline-cpu extra entirely.
"""

import base64
import time

from openai import AsyncOpenAI
from temporalio import activity

from shared.temporal.activity_models import (
    ChandraCallInput,
    ChandraCallOutput,
    LlmTextCallInput,
    LlmTextCallOutput,
)
from shared.vllm.resolve import resolve_vllm_url

from .heartbeat import await_with_heartbeats
from .llm_calls import execute_text_call


def _image_to_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


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
    result = await await_with_heartbeats(
        execute_text_call(
            input.config, input.model, input.prompt,
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
    # Resolve at activity boundary so OCR_VLLM_PREFER_PRIVATE_IP routes in-VPC
    # workers over the private network without the CLI knowing.
    base_url = resolve_vllm_url(input.base_url)
    b64 = _image_to_b64(input.image_path)
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


# Registered with workers that poll the GPU lane (BATCH_GPU_TQ / LIVE_GPU_TQ).
GPU_ACTIVITIES = [
    llm_text_call_activity,
    chandra_vision_call_activity,
]
