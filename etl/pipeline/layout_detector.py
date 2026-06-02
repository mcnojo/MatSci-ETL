from dataclasses import dataclass
import numpy as np
from PIL import Image
from doclayout_yolo import YOLOv10
from huggingface_hub import hf_hub_download

_HF_REPO = "juliozhao/DocLayout-YOLO-DocStructBench"
_HF_FILE = "doclayout_yolo_docstructbench_imgsz1024.pt"


@dataclass
class DetectedElement:
    label: str
    confidence: float
    x0: int
    y0: int
    x1: int
    y1: int
    bbox_norm: tuple[float, float, float, float]


class LayoutDetector:
    def __init__(self, config: dict):
        layout_cfg = config["layout"]
        model_path = layout_cfg.get("model_path")
        if not model_path:
            model_path = hf_hub_download(repo_id=_HF_REPO, filename=_HF_FILE)
        self.model = YOLOv10(model_path)

        self.conf = layout_cfg["confidence_threshold"]
        self.iou = layout_cfg["iou_threshold"]
        self.target_classes = set(layout_cfg["target_classes"])

    def detect(self, image: Image.Image) -> list[DetectedElement]:
        img_array = np.array(image)
        results = self.model.predict(
            img_array,
            imgsz=1024,
            conf=self.conf,
            iou=self.iou,
            verbose=False,
        )

        if not results or len(results) == 0:
            return []

        width, height = image.size
        detected = []
        result = results[0]

        for box in result.boxes:
            label = result.names[int(box.cls)]
            if label not in self.target_classes:
                continue
            x0, y0, x1, y1 = [int(c) for c in box.xyxy[0].tolist()]
            detected.append(DetectedElement(
                label=label,
                confidence=float(box.conf),
                x0=x0, y0=y0, x1=x1, y1=y1,
                bbox_norm=(x0 / width, y0 / height, x1 / width, y1 / height),
            ))

        return detected


def associate_captions(
    elements: list[DetectedElement],
    vertical_threshold: int = 80,
    horizontal_ratio: float = 0.6,
) -> list[tuple[DetectedElement, DetectedElement | None]]:
    """Pair each figure/table/equation with its nearest caption.

    Looks both below and above the content element to handle journals
    where captions appear above their figure.
    """
    content_types = {"figure", "table", "isolate_formula"}
    caption_types = {"figure_caption", "table_caption"}

    content_els = [e for e in elements if e.label in content_types]
    caption_els = [e for e in elements if e.label in caption_types]

    paired = []
    used_captions: set[int] = set()

    for content in content_els:
        best_caption = None
        best_dist = float("inf")
        content_cx = (content.x0 + content.x1) / 2
        content_w = content.x1 - content.x0

        for i, cap in enumerate(caption_els):
            if i in used_captions:
                continue
            cap_cx = (cap.x0 + cap.x1) / 2
            if abs(content_cx - cap_cx) > content_w * horizontal_ratio:
                continue

            # Below: caption top is below content bottom
            dist_below = cap.y0 - content.y1
            # Above: content top is below caption bottom
            dist_above = content.y0 - cap.y1

            if 0 <= dist_below <= vertical_threshold:
                if dist_below < best_dist:
                    best_dist = dist_below
                    best_caption = i
            elif 0 <= dist_above <= vertical_threshold:
                if dist_above < best_dist:
                    best_dist = dist_above
                    best_caption = i

        if best_caption is not None:
            used_captions.add(best_caption)
            paired.append((content, caption_els[best_caption]))
        else:
            paired.append((content, None))

    return paired
