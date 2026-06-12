"""HTTP-bound LLM client for the per-call text activity.

`execute_text_call` is a single-shot HTTP invocation — no internal retry loop.
Temporal's `RetryPolicy` retries the whole activity on failure.

Provider selection:
- ollama: OpenAI-compatible endpoint + native JSON mode (`format: "json"`) + num_ctx
- openai: OpenAI-compatible endpoint (vLLM, MLX, Together, OpenRouter, OpenAI proper)
- anthropic: native Anthropic SDK

Module globals persist across activity invocations within a worker process.
`_init_clients` is idempotent over identical config; `_active_model` is intentionally
preserved so the Ollama VRAM-unload hint can take effect across calls.
"""

import logging
import os
import time

from openai import AsyncOpenAI

from shared.vllm.resolve import resolve_vllm_url

log = logging.getLogger("llm_calls")

_provider: str = "ollama"
_async_client: AsyncOpenAI | None = None
_anthropic_async_client = None
_model_name: str = ""
_num_ctx: int = 32768
_max_response_tokens: int = 8192
_ollama_base: str = ""
_active_model: str | None = None


def _init_clients(config: dict) -> None:
    """Initialize provider clients from `config['tree_llm']`. Mutates module globals.

    Safe to call repeatedly with the same config. Does not reset `_active_model`
    so the cross-call VRAM-unload hint remains effective.
    """
    global _provider, _async_client, _anthropic_async_client
    global _model_name, _num_ctx, _max_response_tokens, _ollama_base

    cfg = config["tree_llm"]
    _model_name = cfg["model"]

    explicit_provider = (cfg.get("provider") or "").lower() or None
    # Resolve here (idempotent for non-vllm-instance URLs) so workers route to
    # the co-hosted tree_llm vLLM unit via the EC2 tag lookup, mirroring the
    # vision activity's resolve at activity boundary.
    base_url = resolve_vllm_url(cfg.get("base_url") or "http://localhost:11434/v1")

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

    _num_ctx = cfg.get("num_ctx") or cfg.get("max_tokens") or 32768
    _max_response_tokens = cfg.get("max_response_tokens", 8192)

    api_key_env = cfg.get("api_key_env")
    api_key = os.environ.get(api_key_env) if api_key_env else None

    _ollama_base = ""

    if _provider == "anthropic":
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ImportError(
                "anthropic provider requires the `anthropic` package."
            ) from e
        if not api_key:
            raise ValueError(
                f"anthropic provider requires api_key_env={api_key_env!r} to be set"
            )
        _anthropic_async_client = AsyncAnthropic(api_key=api_key)
    else:
        _async_client = AsyncOpenAI(base_url=base_url, api_key=api_key or "local")
        if _provider == "ollama":
            _ollama_base = (
                base_url.rsplit("/v1", 1)[0] if "/v1" in base_url else base_url.rstrip("/")
            )


def _ensure_model_exclusive(model_name: str) -> None:
    """Unload the previously active Ollama model before switching. No-op for non-Ollama."""
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


def _openai_messages_to_anthropic(messages: list) -> tuple[str | None, list]:
    """Split OpenAI-style messages into (system, messages) for Anthropic."""
    system_parts = []
    anth_messages = []
    for m in messages:
        if m["role"] == "system":
            system_parts.append(m["content"])
        else:
            anth_messages.append({"role": m["role"], "content": m["content"]})
    return ("\n\n".join(system_parts) if system_parts else None, anth_messages)


def _anthropic_extract(response) -> tuple[str, str]:
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
    if not u:
        return (0, 0)
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def _usage_anthropic(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    if not u:
        return (0, 0)
    return (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


def _openai_chat_kwargs(model: str, messages: list, temperature: float, json_mode: bool) -> dict:
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}
    if _provider == "ollama":
        extra_body: dict = {"num_ctx": _num_ctx}
        if json_mode:
            extra_body["format"] = "json"
        kwargs["extra_body"] = extra_body
    else:
        kwargs["max_tokens"] = _max_response_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
    return kwargs


async def execute_text_call(
    config: dict, model: str, prompt: str,
    *, json_mode: bool = False, temperature: float = 0.0,
) -> dict:
    """One LLM text call. Returns model/content/finish_reason + timing + token usage.

    `started_at` and `ended_at` are `time.time()` (wall-clock, UTC epoch) — required
    so the workflow can compute the *union* of overlapping call intervals.
    `time.perf_counter()` is process-local and would not allow cross-worker union.
    """
    _init_clients(config)
    use_model = model or _model_name
    _ensure_model_exclusive(use_model)

    messages = [{"role": "user", "content": prompt}]
    started_at = time.time()

    if _provider == "anthropic":
        system, anth_messages = _openai_messages_to_anthropic(messages)
        kwargs = {
            "model": use_model,
            "max_tokens": _max_response_tokens,
            "temperature": temperature,
            "messages": anth_messages,
        }
        if system:
            kwargs["system"] = system
        response = await _anthropic_async_client.messages.create(**kwargs)
        content, finish = _anthropic_extract(response)
        in_t, out_t = _usage_anthropic(response)
    else:
        kwargs = _openai_chat_kwargs(use_model, messages, temperature, json_mode)
        response = await _async_client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason or "stop"
        in_t, out_t = _usage_openai(response)

    return {
        "model": use_model,
        "content": content,
        "finish_reason": finish,
        "started_at": started_at,
        "ended_at": time.time(),
        "input_tokens": in_t,
        "output_tokens": out_t,
    }
