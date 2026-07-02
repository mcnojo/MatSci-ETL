"""Regression test: AssetExtractor must not treat output.kb_root as a local FS path.

In prod, `output.kb_root` is an s3:// URI (final tree.json destination). An earlier
version of AssetExtractor did `Path(kb_root) / paper_id / "assets" / "pages"` then
mkdir'd it. `Path("s3://...")` collapses to `s3:/`, so mkdir walked up to the FS
root and exploded with `[Errno 13] Permission denied: 's3:'`. The fix decoupled
the rendering scratch dir from kb_root entirely (per-instance tempdir). This test
locks that contract in so the conflation can't sneak back.
"""

from unittest.mock import patch

from pipeline.asset_extractor import AssetExtractor


def test_instantiates_with_s3_kb_root():
    config = {
        "output": {
            "kb_root": "s3://chem-lit-artifacts/trees",
            "assets_uri_prefix": "s3://chem-lit-artifacts/assets",
            "save_page_images": False,
        },
        "rendering": {"dpi": 150, "ocr_dpi": 300},
    }

    with patch("pipeline.asset_extractor.fitz.open"), \
         patch("pipeline.asset_extractor.LayoutDetector"):
        extractor = AssetExtractor(pdf_path="/dev/null", paper_id="doc-1", config=config)
        try:
            # scratch must resolve to a real local dir, not a stringified s3:// URI
            assert extractor.pages_dir.is_dir()
            assert extractor.elements_dir.is_dir()
            assert not str(extractor.pages_dir).startswith("s3:")
        finally:
            extractor.close()

        # close() must wipe the scratch
        assert not extractor.pages_dir.exists()


if __name__ == "__main__":
    test_instantiates_with_s3_kb_root()
    print("ok")
