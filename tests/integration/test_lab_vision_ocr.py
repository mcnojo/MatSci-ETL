"""Manual harness — vision-OCR quality/format comparison across providers.

NOT collected by pytest (no `def test_*` bodies here). Run directly:

    OPENAI_API_KEY=sk-... python -m tests.integration.test_lab_vision_ocr \\
        --pdf ether_papers/Anion-mediated*.pdf \\
        --pages 4,5 \\
        --provider openai --model gpt-4o \\
        --mode both

For each requested page: crops every layout-detected element (same path as
`ProcessPdfWorkflow._enrich_ocr` — matches production) and/or renders the
whole page, sends each image to the selected vision provider, and writes raw
+ parsed output under `--output-dir` for eyeball inspection. Prints a per-run
summary of tokens + wall-clock so cost is legible.

`element` mode uses `LAB_ELEMENT_OCR_PROMPT` (crop → parseable layout blocks).
`page` mode uses `CHANDRA_OCR_LAYOUT_PROMPT` (full page → chandra-style layout
HTML with real bboxes). Comparing both on the same page shows whether lab
models are worth their (higher) per-call cost on full pages vs. crops.

Provider selection:
  chandra_vllm  — requires a reachable chandra vLLM box (base_url).
  openai        — requires OPENAI_API_KEY env var.
  anthropic     — requires ANTHROPIC_API_KEY env var.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

# Repo-root import path — script mode.
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from pipeline.asset_extractor import AssetExtractor  # noqa: E402
from pipeline.vision_calls import _prompt_for, execute_vision_call  # noqa: E402
from pipeline.vision_parser import parse as parse_vision  # noqa: E402
from shared.prompts.vision import (  # noqa: E402
    CHANDRA_OCR_LAYOUT_PROMPT,
    LAB_ELEMENT_OCR_PROMPT,
)

# Provider → default model. Override with --model.
_DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-5",
    "chandra_vllm": "datalab-to/chandra-ocr-2",
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]


def _synth_config(provider: str, model: str) -> dict:
    """Minimal config: vision_server + the fields AssetExtractor pokes at."""
    vs: dict = {"provider": provider, "ocr_model": model, "max_response_tokens": 4096}
    if provider == "chandra_vllm":
        vs["base_url"] = os.environ.get(
            "CHANDRA_BASE_URL", "vllm-instance://chandra:8004/v1",
        )
        vs["api_key"] = "EMPTY"
    else:
        vs["api_key_env"] = (
            "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        )
    return {
        "vision_server": vs,
        "output": {"assets_uri_prefix": "", "save_page_images": False},
        "rendering": {"dpi": 150, "ocr_dpi": 200},
        "layout": {
            "model_path": None,
            "confidence_threshold": 0.25,
            "iou_threshold": 0.45,
            "target_classes": ["figure", "table", "figure_caption",
                               "table_caption", "isolate_formula"],
        },
    }


def _write_output(out_dir: Path, tag: str, image_path: Path, result: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Copy the image for context alongside its result.
    (out_dir / f"{tag}.png").write_bytes(image_path.read_bytes())
    (out_dir / f"{tag}_raw.txt").write_text(result["content"] or "")
    parsed = parse_vision(result["content"])
    (out_dir / f"{tag}_parsed.json").write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False) if parsed else "null",
    )
    meta = {
        "model": result["model"],
        "elapsed_s": round(result["ended_at"] - result["started_at"], 2),
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "parsed_format": (parsed or {}).get("format"),
    }
    (out_dir / f"{tag}_meta.json").write_text(json.dumps(meta, indent=2))


async def _run_element_mode(
    config: dict, extractor: AssetExtractor, page: int, out_dir: Path,
) -> list[dict]:
    elements = extractor.extract_page_elements(page, node_id="doc")
    results = []
    for elem in elements:
        # asset_uri lives under the extractor's scratch tempdir (assets_uri_prefix=""
        # → put_bytes short-circuits to the local path).
        image_path = Path(elem["asset_uri"])
        if not image_path.exists():
            print(f"    skip {elem['element_id']} — missing crop", flush=True)
            continue
        started = time.time()
        result = await execute_vision_call(config, str(image_path))
        _write_output(out_dir, elem["element_id"], image_path, result)
        results.append({
            "id": elem["element_id"],
            "type": elem["element_type"],
            "elapsed_s": round(time.time() - started, 2),
            "in": result["input_tokens"],
            "out": result["output_tokens"],
        })
        print(
            f"    {elem['element_id']} [{elem['element_type']}] "
            f"{result['input_tokens']:>5} in / {result['output_tokens']:>5} out "
            f"({results[-1]['elapsed_s']}s)",
            flush=True,
        )
    return results


async def _run_page_mode(
    config: dict, extractor: AssetExtractor, page: int, out_dir: Path,
) -> dict | None:
    page_img = extractor.render_page(page, dpi=extractor.ocr_dpi)
    page_path = extractor.pages_dir / f"page_{page:04d}.png"
    page_img.save(str(page_path), "PNG")
    started = time.time()
    result = await execute_vision_call(
        config, str(page_path), prompt=CHANDRA_OCR_LAYOUT_PROMPT,
    )
    _write_output(out_dir, f"page_{page:04d}", page_path, result)
    row = {
        "elapsed_s": round(time.time() - started, 2),
        "in": result["input_tokens"],
        "out": result["output_tokens"],
    }
    print(
        f"    whole-page  {result['input_tokens']:>5} in / {result['output_tokens']:>5} out "
        f"({row['elapsed_s']}s)",
        flush=True,
    )
    return row


async def _amain(args) -> int:
    provider = args.provider
    model = args.model or _DEFAULT_MODELS[provider]

    # Fail fast if credentials are missing — no point renderng pages first.
    if provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set", file=sys.stderr); return 2
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr); return 2

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        print(f"pdf not found: {pdf_path}", file=sys.stderr); return 2
    pages = [int(p) for p in args.pages.split(",")]

    config = _synth_config(provider, model)
    print(f"provider={provider} model={model} prompts: "
          f"element={_prompt_for(provider) is LAB_ELEMENT_OCR_PROMPT and 'lab' or 'chandra'} "
          f"page=chandra_layout")
    print(f"pdf={pdf_path.name}  pages={pages}  mode={args.mode}")

    out_root = Path(args.output_dir).resolve() / _slug(pdf_path.stem) / provider
    print(f"output_dir={out_root}")

    extractor = AssetExtractor(str(pdf_path), _slug(pdf_path.stem), config)
    try:
        summary: dict = {"provider": provider, "model": model, "pages": {}}
        for page in pages:
            print(f"\n=== page {page} ===")
            page_summary: dict = {}
            if args.mode in ("element", "both"):
                el_dir = out_root / f"page_{page:04d}" / "element"
                page_summary["element"] = await _run_element_mode(
                    config, extractor, page, el_dir,
                )
            if args.mode in ("page", "both"):
                pg_dir = out_root / f"page_{page:04d}" / "page"
                page_summary["page"] = await _run_page_mode(
                    config, extractor, page, pg_dir,
                )
            summary["pages"][str(page)] = page_summary

        # Roll-up totals.
        el_in = sum(r["in"] for pg in summary["pages"].values()
                    for r in (pg.get("element") or []))
        el_out = sum(r["out"] for pg in summary["pages"].values()
                     for r in (pg.get("element") or []))
        pg_in = sum((pg["page"] or {}).get("in", 0) for pg in summary["pages"].values()
                    if pg.get("page"))
        pg_out = sum((pg["page"] or {}).get("out", 0) for pg in summary["pages"].values()
                     if pg.get("page"))
        totals = {
            "element_in_tokens": el_in, "element_out_tokens": el_out,
            "page_in_tokens": pg_in,   "page_out_tokens": pg_out,
        }
        summary["totals"] = totals
        (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
        print("\n=== totals ===")
        print(json.dumps(totals, indent=2))
    finally:
        extractor.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pdf", required=True, help="Path to a PDF.")
    p.add_argument("--pages", default="1", help="Comma-separated 1-based page indices.")
    p.add_argument("--provider", choices=list(_DEFAULT_MODELS), default="openai")
    p.add_argument("--model", default=None, help="Override the per-provider default.")
    p.add_argument("--mode", choices=("element", "page", "both"), default="both",
                   help="element = per-crop OCR (matches production); "
                        "page = whole-page OCR with chandra layout prompt.")
    p.add_argument("--output-dir", default="tests/integration/lab_vision_output")
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
