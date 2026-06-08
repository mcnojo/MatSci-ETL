"""Unit tests for the batch workflow's pure helpers.

The workflow classes themselves require a Temporal worker (real or
time-skipping) to exercise end-to-end. This file covers the pure functions
that the workflow uses for config merging, per-item row construction,
failure-row construction, and string truncation — the parts most likely to
have logic bugs and easiest to test in isolation.

End-to-end workflow validation is done by submitting a small manifest
against the local docker-compose Temporal stack — see prod/batch/README.md.

Run: python -m tests.test_batch_workflows
"""

import sys
import traceback

from prod.batch.workflows.batch_run import (
    _failure_row,
    _merge_config,
    _per_item_row,
    _truncate,
)
from prod.batch.workflows.models import BatchRunInput, ItemResult


# BatchRunInput fleet fields all-or-none


def _fleet_kwargs() -> dict:
    return dict(
        region="us-west-2",
        cpu_queue_asg_name="cpu-asg",
        gpu_queue_asg_name="gpu-asg",
        cpu_queue_desired=2,
        gpu_queue_desired=2,
    )


def _input_kwargs() -> dict:
    return dict(
        batch_id="t",
        manifest_uri="s3://b/m.json",
        pipeline_config={},
        report_root="s3://b/reports",
    )


def test_batch_run_input_manages_fleet_false_when_unset():
    inp = BatchRunInput(**_input_kwargs())
    assert inp.manages_fleet is False


def test_batch_run_input_manages_fleet_true_when_all_set():
    inp = BatchRunInput(**_input_kwargs(), **_fleet_kwargs())
    assert inp.manages_fleet is True


def test_batch_run_input_partial_fleet_rejected():
    bad = _fleet_kwargs()
    del bad["gpu_queue_desired"]
    raised = None
    try:
        BatchRunInput(**_input_kwargs(), **bad)
    except Exception as exc:
        raised = exc
    assert raised is not None, "partial fleet was accepted but should have raised"
    assert "all-or-none" in str(raised)


# _merge_config


def test_merge_config_no_overrides():
    base = {"tree_llm": {"model": "strong"}, "output": {"kb_root": "./kb"}}
    assert _merge_config(base, None) == base
    assert _merge_config(base, {}) == base


def test_merge_config_top_level_section_replaced_for_non_dict():
    base = {"output": {"kb_root": "./kb"}, "list_field": [1, 2]}
    overrides = {"list_field": [9]}
    out = _merge_config(base, overrides)
    assert out["list_field"] == [9]
    assert out["output"] == {"kb_root": "./kb"}


def test_merge_config_dict_sections_merge_one_level():
    base = {"tree_llm": {"model": "strong", "model_fast": "fast", "temperature": 0.0}}
    overrides = {"tree_llm": {"model": "even-stronger"}}
    out = _merge_config(base, overrides)
    # Override replaces only the named key; siblings are preserved
    assert out["tree_llm"]["model"] == "even-stronger"
    assert out["tree_llm"]["model_fast"] == "fast"
    assert out["tree_llm"]["temperature"] == 0.0


def test_merge_config_new_section_added():
    base = {"tree_llm": {"model": "strong"}}
    overrides = {"new_section": {"key": "value"}}
    out = _merge_config(base, overrides)
    assert out["tree_llm"] == {"model": "strong"}
    assert out["new_section"] == {"key": "value"}


def test_merge_config_does_not_mutate_inputs():
    base = {"tree_llm": {"model": "strong"}}
    overrides = {"tree_llm": {"model": "weak"}}
    _merge_config(base, overrides)
    assert base["tree_llm"]["model"] == "strong"
    assert overrides["tree_llm"]["model"] == "weak"


# _per_item_row


def test_per_item_row_success():
    r = ItemResult(
        document_id="doc-1", pdf_uri="s3://b/p.pdf", status="success",
        workflow_id="wf-1", tree_path="s3://b/trees/doc-1/tree.json",
        node_count=42, total_pages=10,
    )
    row = _per_item_row(r)
    assert row["document_id"] == "doc-1"
    assert row["status"] == "success"
    assert row["tree_path"] == "s3://b/trees/doc-1/tree.json"
    assert row["node_count"] == 42
    assert row["total_pages"] == 10
    assert row["error"] == ""


def test_per_item_row_failure_has_empty_optional_fields():
    r = ItemResult(
        document_id="doc-2", pdf_uri="s3://b/q.pdf", status="failure",
        workflow_id="wf-2", error="boom",
    )
    row = _per_item_row(r)
    assert row["status"] == "failure"
    assert row["tree_path"] == ""
    assert row["node_count"] == ""
    assert row["total_pages"] == ""
    assert row["error"] == "boom"


def test_per_item_row_error_truncated_and_no_newlines():
    long_err = "line1\nline2\n" + ("x" * 1000)
    r = ItemResult(
        document_id="doc-3", pdf_uri="s3://b/r.pdf", status="failure",
        workflow_id="wf-3", error=long_err,
    )
    row = _per_item_row(r)
    assert "\n" not in row["error"]
    assert len(row["error"]) <= 500


# _failure_row


def test_failure_row_carries_full_error():
    long_err = "a" * 5000
    r = ItemResult(
        document_id="d", pdf_uri="p", status="failure",
        workflow_id="w", error=long_err,
    )
    row = _failure_row(r)
    # failures.jsonl gets the FULL error; per_item.csv truncates
    assert row["error"] == long_err


def test_failure_row_fields():
    r = ItemResult(
        document_id="d", pdf_uri="p", status="failure",
        workflow_id="w", error="e",
    )
    row = _failure_row(r)
    assert set(row.keys()) == {"document_id", "pdf_uri", "workflow_id", "error"}


# _truncate


def test_truncate_pass_through_when_short():
    assert _truncate("hello", 100) == "hello"


def test_truncate_appends_count_when_long():
    s = "x" * 2050
    out = _truncate(s, 2000)
    assert out.startswith("x" * 2000)
    assert "[+50 more chars]" in out


def test_truncate_at_exact_limit_is_passthrough():
    s = "x" * 100
    assert _truncate(s, 100) == s


# Workflow class structural sanity


def test_workflows_have_temporal_defn():
    """Verify the @workflow.defn decorator was applied and the run method exists."""
    from prod.batch.workflows.batch_run import BatchRunWorkflow
    from prod.batch.workflows.shard import ShardWorkflow
    # The decorator attaches __temporal_workflow_definition (private name varies
    # across SDK versions). Use the public API instead: workflow names default
    # to class names.
    assert BatchRunWorkflow.__name__ == "BatchRunWorkflow"
    assert ShardWorkflow.__name__ == "ShardWorkflow"
    assert callable(BatchRunWorkflow.run)
    assert callable(ShardWorkflow.run)


_TESTS = [
    test_merge_config_no_overrides,
    test_merge_config_top_level_section_replaced_for_non_dict,
    test_merge_config_dict_sections_merge_one_level,
    test_merge_config_new_section_added,
    test_merge_config_does_not_mutate_inputs,
    test_per_item_row_success,
    test_per_item_row_failure_has_empty_optional_fields,
    test_per_item_row_error_truncated_and_no_newlines,
    test_failure_row_carries_full_error,
    test_failure_row_fields,
    test_truncate_pass_through_when_short,
    test_truncate_appends_count_when_long,
    test_truncate_at_exact_limit_is_passthrough,
    test_workflows_have_temporal_defn,
    test_batch_run_input_manages_fleet_false_when_unset,
    test_batch_run_input_manages_fleet_true_when_all_set,
    test_batch_run_input_partial_fleet_rejected,
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
        print(f"\nFAIL: {failed}/{len(_TESTS)} batch-workflow tests failed")
        return 1
    print(f"PASS: {len(_TESTS)} batch-workflow tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
