"""Unit tests for the batch planner.

The planner is pure (no Temporal, no S3) so it's the right level to lock in
sharding invariants: contiguity, completeness, and bounded size. Workflow ID
derivation is also pinned here because changing it would invalidate every
in-flight workflow.

Run: python -m tests.test_batch_planner
"""

import sys
import traceback

from prod.batch.models import BatchItem, BatchManifest
from prod.batch.planner import (
    DEFAULT_SHARD_SIZE,
    per_pdf_workflow_id,
    shard_manifest,
    shard_workflow_id,
)


def _manifest(n: int, *, batch_id: str = "test-batch") -> BatchManifest:
    return BatchManifest(
        batch_id=batch_id,
        items=[
            BatchItem(document_id=f"doc-{i:04d}", pdf_uri=f"s3://b/p{i}.pdf")
            for i in range(n)
        ],
    )


def test_shard_count_matches_ceil_division():
    assert len(shard_manifest(_manifest(50), shard_size=50)) == 1
    assert len(shard_manifest(_manifest(51), shard_size=50)) == 2
    assert len(shard_manifest(_manifest(100), shard_size=50)) == 2
    assert len(shard_manifest(_manifest(123), shard_size=50)) == 3


def test_shards_are_contiguous_and_complete():
    """Items should be reassembled in original order by concatenating shards."""
    m = _manifest(123)
    shards = shard_manifest(m, shard_size=50)
    rebuilt = [item for shard in shards for item in shard]
    assert rebuilt == list(m.items)


def test_shard_bounded_size():
    m = _manifest(123)
    shards = shard_manifest(m, shard_size=50)
    assert all(len(s) <= 50 for s in shards)
    # Only the last shard may be partial; all others must be full
    assert all(len(s) == 50 for s in shards[:-1])


def test_shard_size_larger_than_items_collapses_to_one():
    shards = shard_manifest(_manifest(5), shard_size=50)
    assert len(shards) == 1
    assert len(shards[0]) == 5


def test_default_shard_size_is_50():
    assert DEFAULT_SHARD_SIZE == 50
    # The DEFAULT should round-trip through the function unchanged
    shards = shard_manifest(_manifest(50))
    assert len(shards) == 1


def test_invalid_shard_size_raises():
    try:
        shard_manifest(_manifest(10), shard_size=0)
    except ValueError:
        pass
    else:
        raise AssertionError("shard_size=0 should raise")

    try:
        shard_manifest(_manifest(10), shard_size=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("shard_size=-1 should raise")


def test_workflow_id_format():
    """Workflow IDs are durable identifiers — pin the format to catch
    accidental renames that would orphan in-flight workflows."""
    assert shard_workflow_id("q2-corpus", 0) == "batch-q2-corpus-shard-0000"
    assert shard_workflow_id("q2-corpus", 42) == "batch-q2-corpus-shard-0042"
    assert shard_workflow_id("q2-corpus", 1234) == "batch-q2-corpus-shard-1234"

    assert per_pdf_workflow_id("q2-corpus", "j-acs-001") == "batch-q2-corpus-pdf-j-acs-001"


def test_manifest_validation_duplicate_document_id():
    try:
        BatchManifest(
            batch_id="b",
            items=[
                BatchItem(document_id="x", pdf_uri="s3://a/1.pdf"),
                BatchItem(document_id="x", pdf_uri="s3://a/2.pdf"),
            ],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate document_id should fail validation")


def test_manifest_validation_empty_items():
    try:
        BatchManifest(batch_id="b", items=[])
    except ValueError:
        pass
    else:
        raise AssertionError("empty items list should fail validation")


def test_manifest_validation_invalid_document_id_chars():
    try:
        BatchItem(document_id="has/slashes", pdf_uri="s3://a/1.pdf")
    except ValueError:
        pass
    else:
        raise AssertionError("document_id with '/' should fail validation")


_TESTS = [
    test_shard_count_matches_ceil_division,
    test_shards_are_contiguous_and_complete,
    test_shard_bounded_size,
    test_shard_size_larger_than_items_collapses_to_one,
    test_default_shard_size_is_50,
    test_invalid_shard_size_raises,
    test_workflow_id_format,
    test_manifest_validation_duplicate_document_id,
    test_manifest_validation_empty_items,
    test_manifest_validation_invalid_document_id_chars,
]


def main() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    if failed:
        print(f"\nFAIL: {failed}/{len(_TESTS)} planner tests failed")
        return 1
    print(f"PASS: {len(_TESTS)} planner tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
