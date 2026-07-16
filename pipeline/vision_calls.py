"""Provider-dispatched vision OCR for element-crop inputs.

`vision_server.provider` selects the wire path + prompt:
  chandra_vllm — datalab-to/chandra-ocr-2 on an OpenAI-compatible vLLM box.
  openai       — OpenAI vision (gpt-4o, gpt-4o-mini, ...).
  anthropic    — Anthropic vision (Claude 4.5+).

All three normalize onto the same output shape (chandra layout_html OR
<analyze> figure_analysis blocks); `pipeline.vision_parser` handles both.
"""

import asyncio
import base64
import logging
import os
import time

import litellm

from shared.prompts.vision import CHANDRA_OCR_LAYOUT_PROMPT, LAB_ELEMENT_OCR_PROMPT
from shared.s3_io import get_bytes
from shared.vllm.resolve import resolve_vllm_url

log = logging.getLogger("vision_calls")

# Matches llm_calls: outer httpx envelope < Temporal start_to_close.
VISION_REQUEST_TIMEOUT_S = 25 * 60

_VALID_PROVIDERS = ("chandra_vllm", "openai", "anthropic")


def _image_to_b64(image_uri: str) -> str:
    return base64.b64encode(get_bytes(image_uri)).decode("utf-8")


def _prompt_for(provider: str) -> str:
    # Chandra was trained on the layout-labels vocabulary; lab models need the
    # explicit mode-A/mode-B schema to reach the same output contract.
    return CHANDRA_OCR_LAYOUT_PROMPT if provider == "chandra_vllm" else LAB_ELEMENT_OCR_PROMPT


def _litellm_route(cfg: dict, provider: str) -> tuple[str, dict]:
    """Return (litellm-model-string, provider-specific kwargs)."""
    model = cfg["ocr_model"]
    max_tokens = int(cfg.get("max_response_tokens", 8192))
    kwargs: dict = {
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "timeout": VISION_REQUEST_TIMEOUT_S,
        "_skip_mcp_handler": True,
    }
    if provider == "chandra_vllm":
        kwargs["api_base"] = resolve_vllm_url(cfg["base_url"])
        kwargs["api_key"] = cfg.get("api_key") or "local"
        return f"openai/{model}", kwargs
    if provider == "openai":
        api_key = os.environ.get(cfg["api_key_env"])
        if not api_key:
            raise ValueError(f"openai vision: {cfg['api_key_env']} not set")
        base_url = cfg.get("base_url")
        if base_url:
            kwargs["api_base"] = base_url
        kwargs["api_key"] = api_key
        return f"openai/{model}", kwargs
    # anthropic
    api_key = os.environ.get(cfg["api_key_env"])
    if not api_key:
        raise ValueError(f"anthropic vision: {cfg['api_key_env']} not set")
    kwargs["api_key"] = api_key
    return f"anthropic/{model}", kwargs


def _messages(prompt: str, b64: str) -> list[dict]:
    # OpenAI-shape content parts; litellm translates for anthropic transparently.
    return [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ],
    }]


def _usage(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    if not u:
        return (0, 0)
    return (
        int(getattr(u, "prompt_tokens", 0) or 0),
        int(getattr(u, "completion_tokens", 0) or 0),
    )


async def execute_vision_call(
    config: dict, image_uri: str, *, prompt: str | None = None,
) -> dict:
    """One vision OCR call. Returns model/content/timing/token-usage.

    `prompt` override skips provider-based dispatch — used by manual harnesses
    that compare whole-page vs. element-crop modes; production callers omit it.
    """
    cfg = config["vision_server"]
    provider = (cfg.get("provider") or "chandra_vllm").lower()
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"vision_server.provider must be one of {_VALID_PROVIDERS}, got {provider!r}"
        )

    litellm_model, kwargs = _litellm_route(cfg, provider)
    if prompt is None:
        prompt = _prompt_for(provider)
    b64 = _image_to_b64(image_uri)

    started_at = time.time()
    response = await asyncio.wait_for(
        litellm.acompletion(
            model=litellm_model,
            messages=_messages(prompt, b64),
            **kwargs,
        ),
        timeout=VISION_REQUEST_TIMEOUT_S,
    )
    ended_at = time.time()

    content = (response.choices[0].message.content or "") if response.choices else ""
    in_t, out_t = _usage(response)
    return {
        "model": cfg["ocr_model"],
        "content": content,
        "started_at": started_at,
        "ended_at": ended_at,
        "input_tokens": in_t,
        "output_tokens": out_t,
    }
