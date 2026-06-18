from pathlib import Path
from typing import Callable
import fitz  # PyMuPDF
from PIL import Image

from shared.s3_io import put_bytes

from .layout_detector import LayoutDetector, associate_captions


class AssetExtractor:
    """Renders PDF pages and crops detected visual elements.

    Every PNG is written to local scratch (fast cache for the producing worker)
    and also published to `assets_uri_prefix` via `shared.s3_io.put_bytes`. The
    returned URI is the durable cross-worker reference — consumers on other
    workers (e.g. GPU chandra) reach the bytes through s3_io.get_bytes.

    Local-dev mode: `assets_uri_prefix` is a filesystem path under `kb_root`,
    so `put_bytes` writes locally and the URI is just that local path. No code
    branches on prefix scheme.
    """

    def __init__(self, pdf_path: str, paper_id: str, config: dict):
        self.pdf_path = pdf_path
        self.paper_id = paper_id
        self.config = config
        self.kb_root = Path(config["output"]["kb_root"])
        self.assets_uri_prefix = config["output"]["assets_uri_prefix"].rstrip("/")
        self.render_dpi = config["rendering"]["dpi"]
        self.ocr_dpi = config["rendering"]["ocr_dpi"]
        self.save_pages = config["output"]["save_page_images"]

        self.paper_dir = self.kb_root / paper_id
        self.pages_dir = self.paper_dir / "assets" / "pages"
        self.elements_dir = self.paper_dir / "assets" / "elements"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.elements_dir.mkdir(parents=True, exist_ok=True)

        self.doc = fitz.open(pdf_path)
        self.detector = LayoutDetector(config)

    def close(self):
        self.doc.close()

    def render_page(self, page_1based: int, dpi: int | None = None) -> Image.Image:
        dpi = dpi or self.render_dpi
        page = self.doc[page_1based - 1]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    def _publish(self, local_path: Path, key_suffix: str) -> str:
        """Write local cache + publish to URI prefix; return URI."""
        uri = f"{self.assets_uri_prefix}/{self.paper_id}/{key_suffix}"
        body = local_path.read_bytes()
        put_bytes(uri, body, "image/png")
        return uri

    def save_page_image(self, page_1based: int) -> str:
        path = self.pages_dir / f"p{page_1based:04d}.png"
        if not path.exists():
            img = self.render_page(page_1based)
            img.save(str(path), "PNG")
        return self._publish(path, f"pages/p{page_1based:04d}.png")

    def extract_page_elements(self, page_1based: int, node_id: str) -> list[dict]:
        """Run layout detection on one page and crop each detected element."""
        page_img = self.render_page(page_1based)
        elements = self.detector.detect(page_img)
        paired = associate_captions(elements)

        ocr_page_img = self.render_page(page_1based, dpi=self.ocr_dpi)
        scale = self.ocr_dpi / self.render_dpi

        results = []
        counters: dict[str, int] = {}

        for content_el, caption_el in paired:
            label = content_el.label
            counters[label] = counters.get(label, 0) + 1
            prefix = {
                "figure": "fig",
                "table": "tbl",
                "isolate_formula": "eq",
            }.get(label, "el")
            element_id = f"{prefix}_{node_id}_{page_1based:04d}_{counters[label]:03d}"

            # Crop from high-res image
            sx0 = int(content_el.x0 * scale)
            sy0 = int(content_el.y0 * scale)
            sx1 = int(content_el.x1 * scale)
            sy1 = int(content_el.y1 * scale)
            crop = ocr_page_img.crop((sx0, sy0, sx1, sy1))

            local_asset_path = self.elements_dir / f"{element_id}.png"
            crop.save(str(local_asset_path), "PNG")
            asset_uri = self._publish(local_asset_path, f"elements/{element_id}.png")

            # Extract caption text via PyMuPDF if a caption region was detected
            caption_text = None
            if caption_el is not None:
                page = self.doc[page_1based - 1]
                pts_scale = 72 / self.render_dpi
                rect = fitz.Rect(
                    caption_el.x0 * pts_scale,
                    caption_el.y0 * pts_scale,
                    caption_el.x1 * pts_scale,
                    caption_el.y1 * pts_scale,
                )
                caption_text = page.get_text("text", clip=rect).strip() or None

            results.append({
                "element_id": element_id,
                "element_type": label,
                "page_index": page_1based,
                "bbox": {
                    "x0": content_el.bbox_norm[0],
                    "y0": content_el.bbox_norm[1],
                    "x1": content_el.bbox_norm[2],
                    "y1": content_el.bbox_norm[3],
                },
                "asset_uri": asset_uri,
                "caption": caption_text,
                "ocr_text": None,
                "chem_entities": [],
                "structured_data": None,
            })

        return results

    def extract_all_pages(
        self, page_range: set[int],
        heartbeat: Callable[..., None] | None = None,
    ) -> tuple[dict[int, list[dict]], dict[int, str]]:
        """Process all requested pages.

        Returns (page_elements, page_image_uris) where:
        - page_elements: page_index -> list of element dicts (each with asset_uri)
        - page_image_uris: page_index -> URI of the rendered full-page PNG
          (only populated when save_page_images is true)

        `heartbeat(page_idx, total)` is fired once per page so a Temporal activity
        wrapper can pass `activity.heartbeat` and avoid the worker's heartbeat
        timeout — render + layout-detect on a dense page can exceed the cap.
        Default no-op keeps non-Temporal callers (etl/cli) untouched.
        """
        page_elements: dict[int, list[dict]] = {}
        page_image_uris: dict[int, str] = {}
        ordered = sorted(p for p in page_range if 1 <= p <= len(self.doc))
        total = len(ordered)
        for page_idx in ordered:
            if heartbeat is not None:
                heartbeat(page_idx, total)
            if self.save_pages:
                page_image_uris[page_idx] = self.save_page_image(page_idx)
            elems = self.extract_page_elements(page_idx, node_id="doc")
            if elems:
                page_elements[page_idx] = elems
        return page_elements, page_image_uris
