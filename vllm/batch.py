from pathlib import Path
from client import RESULTS_DIR, ocr_image, _resolve_host

IMAGES = [
    "../data/pages/1-s2.0-S0378775320310466-main/9/doclayout_yolo/figures/figure_0.png",
    "../data/pages/20120016539/13/doclayout_yolo/figures/figure_0.png",
    "../data/pages/am2c09841/5/doclayout_yolo/figures/figure_0.png",
    "../data/pages/Cathode_materials_for_secondary_(rechargeable)_lithium_batteriesUS6514640/7/doclayout_yolo/figures/figure_0.png",
]


def run(model="chandra", task="chart"):
    host = _resolve_host(model, None)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for img in IMAGES:
        result = ocr_image(img, model, host, task=task)
        p = Path(img)
        # ../data/pages/<pdf_stem>/<page>/doclayout_yolo/figures/<img>.png
        pdf_stem = p.parts[-5]
        page = p.parts[-4]
        out = RESULTS_DIR / f"{pdf_stem}_p{page}_{p.stem}_{model}_{task}.md"
        out.write_text(result["text"])
        print(f"{img} -> {out.name} ({result['elapsed_s']}s)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="chandra")
    p.add_argument("--task", default="chart")
    args = p.parse_args()
    run(model=args.model, task=args.task)
