import asyncio
import base64
import re
import time
from pathlib import Path
from openai import AsyncOpenAI

from shared.schemas import TreeNode, DocumentTree, VisualElement, NodeSource
from shared.prompts.chandra import CHANDRA_OCR_LAYOUT_PROMPT

from .chem_extractor import extract_chem_entities, load_seed_entities
from .chandra_parser import parse as parse_chandra
from .metrics import get_current_metrics


def _image_to_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _record_vision_call(
    label: str, duration_s: float, errored: bool,
    input_tokens: int = 0, output_tokens: int = 0,
) -> None:
    """Push an OCR call timing into the current PipelineMetrics if one is set."""
    m = get_current_metrics()
    if m is not None:
        m.record_llm_call(
            label, duration_s, error=errored,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )


def _openai_usage(response) -> tuple[int, int]:
    u = getattr(response, "usage", None)
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0) if u else (0, 0)


class Enricher:
    """Enriches visual elements via OCR."""

    def __init__(self, config: dict):
        vllm_cfg = config["vision_server"]
        self.client = AsyncOpenAI(
            base_url=vllm_cfg["base_url"],
            api_key=vllm_cfg["api_key"],
        )
        self.ocr_model = vllm_cfg["ocr_model"]
        self.semaphore = asyncio.Semaphore(
            config["enrichment"]["max_concurrent_enrichments"]
        )

    async def _ocr_extract(self, image_path: str) -> str:
        b64 = _image_to_b64(image_path)
        t0 = time.perf_counter()
        errored = False
        usage = (0, 0)
        try:
            async with self.semaphore:
                response = await self.client.chat.completions.create(
                    model=self.ocr_model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                                {"type": "text", "text": CHANDRA_OCR_LAYOUT_PROMPT},
                            ],
                        },
                    ],
                    max_tokens=4096,
                    temperature=0.0,
                )
            usage = _openai_usage(response)
            return response.choices[0].message.content
        except Exception:
            errored = True
            raise
        finally:
            _record_vision_call(
                f"ocr:{self.ocr_model}", time.perf_counter() - t0, errored,
                input_tokens=usage[0], output_tokens=usage[1],
            )

    async def enrich_element(self, element: dict, run_ocr: bool) -> dict:
        if not (run_ocr and element.get("asset_path")):
            return element
        try:
            element["ocr_text"] = await self._ocr_extract(element["asset_path"])
        except Exception as e:
            element["ocr_text"] = f"ERROR: {e}"
            return element
        element["ocr_parsed"] = parse_chandra(element["ocr_text"])
        return element

    async def enrich_all(
        self, page_elements: dict[int, list[dict]], config: dict
    ) -> dict[int, list[dict]]:
        run_ocr = config["enrichment"]["run_ocr"]

        all_tasks = []
        for elements in page_elements.values():
            for elem in elements:
                all_tasks.append(self.enrich_element(elem, run_ocr))

        await asyncio.gather(*all_tasks)
        return page_elements


# Tree enrichment: assign elements to nodes

def flatten_tree(nodes: list[TreeNode]) -> list[TreeNode]:
    result = []
    for node in nodes:
        result.append(node)
        if node.nodes:
            result.extend(flatten_tree(node.nodes))
    return result


def assign_elements_to_tree(
    tree: DocumentTree,
    page_elements: dict[int, list[dict]],
    pdf_path: str,
    pages_dir: Path,
    config: dict,
) -> DocumentTree:
    """Attach visual elements to the deepest tree node whose page range contains them."""
    seed_entities = load_seed_entities(
        str(Path(config.get("_config_dir", "config")) / "chem_entities.yaml")
    )
    flat_nodes = flatten_tree(tree.root_nodes)

    # Build page -> deepest node mapping (later = deeper in DFS order)
    page_to_node: dict[int, TreeNode] = {}
    for node in flat_nodes:
        for p in range(node.start_index, node.end_index + 1):
            page_to_node[p] = node

    # Populate NodeSource
    save_pages = config["output"]["save_page_images"]
    for node in flat_nodes:
        page_image_paths = []
        if save_pages:
            for p in range(node.start_index, node.end_index + 1):
                img_path = pages_dir / f"p{p:04d}.png"
                if img_path.exists():
                    page_image_paths.append(str(img_path.resolve()))
        node.source = NodeSource(
            pdf_path=str(Path(pdf_path).resolve()),
            paper_id=tree.paper_id,
            page_images=page_image_paths,
        )

    # Assign elements
    run_chem = config["enrichment"]["run_chem_entity_extraction"]
    for page_idx, elements in page_elements.items():
        target_node = page_to_node.get(page_idx)
        if target_node is None:
            continue
        for elem_dict in elements:
            if run_chem:
                combined_text = " ".join(filter(None, [
                    elem_dict.get("ocr_text", ""),
                    elem_dict.get("caption", ""),
                ]))
                elem_dict["chem_entities"] = extract_chem_entities(combined_text, seed_entities)

            # For tables: pull the first <table>…</table> out of chandra's layout_html OCR.
            if elem_dict["element_type"] == "table":
                ocr = elem_dict.get("ocr_text") or ""
                m = re.search(r"<table[\s\S]*?</table>", ocr, re.IGNORECASE)
                if m:
                    elem_dict["structured_data"] = m.group(0)

            target_node.visual_elements.append(VisualElement(**elem_dict))

    return tree
