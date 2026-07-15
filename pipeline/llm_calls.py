"""HTTP-bound LLM client. Text and structured variants share a litellm transport.

`execute_text_call` returns raw content; `execute_structured_call` runs the same
call through instructor and returns a validated Pydantic model as a dict (fence
stripping, JSON coercion, and repair-on-ValidationError are inside instructor —
no hand-rolled fallback in the workflow). Neither retries transport failures —
Temporal's activity RetryPolicy owns that layer.
"""

import asyncio
import logging
import os
import time
from typing import Any

import instructor
import litellm
from pydantic import BaseModel

from shared.vllm.resolve import resolve_vllm_url

log = logging.getLogger("llm_calls")

# instructor's validation-repair budget. One repair round handles the common
# "Yes/No case mismatch" / "unwrapped array" retries; more than that usually
# means the schema is wrong, not the model.
_INSTRUCTOR_MAX_RETRIES = 3

# Per-call ceiling; under GPU_ACTIVITY_TIMEOUT so Temporal doesn't cancel first.
LLM_REQUEST_TIMEOUT_S = 25 * 60


def _completion_kwargs(config: dict, model: str) -> tuple[str, dict]:
    """Build (litellm-model-string, common kwargs) from `config['tree_llm']`.

    Prefix maps provider → litellm route: `openai/*` hits any OpenAI-compatible
    endpoint (vLLM, OpenAI, together) via `api_base`; `anthropic/*` uses the
    native SDK. Caller adds messages / temperature / response_format.
    """
    cfg = config["tree_llm"]
    provider = (cfg.get("provider") or "openai").lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError(
            f"tree_llm.provider must be 'openai' | 'anthropic', got {provider!r}"
        )

    api_key_env = cfg.get("api_key_env")
    api_key = os.environ.get(api_key_env) if api_key_env else None
    max_tokens = int(cfg.get("max_response_tokens", 8192))

    litellm_model = f"{provider}/{model}"
    # `_skip_mcp_handler`: litellm.main.completion:4866 gates on `if not
    # skip_mcp_handler and tools:` before importing litellm.responses.mcp, which
    # transitively pulls litellm.proxy (fastapi, orjson, redis…). Instructor's
    # default TOOLS mode always sets `tools=[...]`, so every structured call
    # trips that chain unless we opt out here. We never route to an MCP gateway,
    # so opting out is correct — the escape-hatch is litellm's own kwarg.
    kwargs: dict[str, Any] = {
        "max_tokens": max_tokens,
        "_skip_mcp_handler": True,
        "timeout": LLM_REQUEST_TIMEOUT_S,
    }

    if provider == "openai":
        # Any non-api.openai.com OpenAI-compatible endpoint needs api_base.
        kwargs["api_base"] = resolve_vllm_url(cfg["base_url"])
        kwargs["api_key"] = api_key or "local"
    else:
        if not api_key:
            raise ValueError(
                f"anthropic provider requires api_key_env={api_key_env!r} to be set"
            )
        kwargs["api_key"] = api_key

    return litellm_model, kwargs


def _usage(response) -> tuple[int, int]:
    """litellm normalizes response shape to OpenAI's usage.{prompt,completion}_tokens."""
    u = getattr(response, "usage", None)
    if not u:
        return (0, 0)
    return (
        int(getattr(u, "prompt_tokens", 0) or 0),
        int(getattr(u, "completion_tokens", 0) or 0),
    )


async def execute_text_call(
    config: dict, model: str, prompt: str,
    *, json_mode: bool = False, temperature: float = 0.0,
) -> dict:
    """One LLM text call. Returns model/content/finish_reason + timing + token usage.

    `started_at` / `ended_at` are wall-clock (`time.time()`) so the workflow can
    compute the *union* of overlapping call intervals across workers —
    `perf_counter()` is process-local and would break that.
    """
    litellm_model, kwargs = _completion_kwargs(config, model)
    kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    started_at = time.time()
    # Hard envelope — httpx read timeout can be defeated by TCP keepalive.
    response = await asyncio.wait_for(
        litellm.acompletion(
            model=litellm_model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        ),
        timeout=LLM_REQUEST_TIMEOUT_S,
    )
    ended_at = time.time()

    choice = response.choices[0]
    content = (choice.message.content or "") if choice.message else ""
    finish = choice.finish_reason or "stop"
    # Anthropic reports max_tokens; OpenAI reports length — collapse for callers.
    if finish == "max_tokens":
        finish = "length"

    in_t, out_t = _usage(response)
    return {
        "model": model,
        "content": content,
        "finish_reason": finish,
        "started_at": started_at,
        "ended_at": ended_at,
        "input_tokens": in_t,
        "output_tokens": out_t,
    }


async def execute_structured_call(
    config: dict, model: str, prompt: str, response_model: type[BaseModel],
    *, temperature: float = 0.0,
) -> dict:
    """LLM call returning a validated Pydantic model dumped to a dict.

    instructor drives provider-native structured output (Anthropic tool-use,
    OpenAI json_schema/json_object) and repairs the response on ValidationError
    up to `_INSTRUCTOR_MAX_RETRIES` times. The dict return preserves the
    activity-boundary contract — Pydantic classes don't cross Temporal
    serialization; the workflow reads `data` by key.
    """
    litellm_model, kwargs = _completion_kwargs(config, model)
    kwargs["temperature"] = temperature

    # Mode picks the wire format instructor emits for schema-constrained output.
    # vLLM's OpenAI-compat frontend has no `--tool-call-parser` wired by default
    # (and gemma-3n isn't tool-call-trained), so TOOLS mode gets rejected server
    # -side; JSON_SCHEMA rides vLLM's built-in guided-decoding backend and works
    # with no server config. Anthropic uses its native tool-use protocol.
    provider = (config["tree_llm"].get("provider") or "openai").lower()
    mode = (
        instructor.Mode.JSON_SCHEMA if provider == "openai"
        else instructor.Mode.ANTHROPIC_TOOLS
    )
    client = instructor.from_litellm(litellm.acompletion, mode=mode)
    started_at = time.time()
    # Envelope covers all instructor retries, not just one httpx call.
    try:
        parsed, raw = await asyncio.wait_for(
            client.chat.completions.create_with_completion(
                model=litellm_model,
                messages=[{"role": "user", "content": prompt}],
                response_model=response_model,
                max_retries=_INSTRUCTOR_MAX_RETRIES,
                **kwargs,
            ),
            timeout=LLM_REQUEST_TIMEOUT_S,
        )
    except Exception as exc:
        # Instructor swallows the raw model output on retry-exhaust; surface
        # last_completion if attached, else the prompt tail, so we can diagnose
        # "gemma emitted the schema minimum" without adding a wire trace.
        last = getattr(exc, "last_completion", None)
        raw_tail = (
            (last.choices[0].message.content or "")[-500:]
            if last and getattr(last, "choices", None) else "<unavailable>"
        )
        log.warning(
            "execute_structured_call failed schema=%s model=%s: %s | raw_tail=%r | prompt_tail=%r",
            response_model.__name__, model, exc, raw_tail, prompt[-300:],
        )
        raise
    ended_at = time.time()

    in_t, out_t = _usage(raw)
    return {
        "model": model,
        "data": parsed.model_dump(),
        "started_at": started_at,
        "ended_at": ended_at,
        "input_tokens": in_t,
        "output_tokens": out_t,
    }
