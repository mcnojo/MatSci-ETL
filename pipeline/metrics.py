"""Pipeline run metrics — workflow-side aggregation only.

Per-call activities (`llm_text_call_activity`, `vision_ocr_call_activity`)
return `started_at` and `ended_at` Unix timestamps. The workflow's call_llm
closure (`prod/workflows/process_pdf.py`) records each interval via
`merge_call_record`. Before the workflow returns its output, it calls
`finalize_summary`, which collapses the per-model interval lists into:

  - compute_time_s   — sum of per-call durations (a model's total work)
  - wall_clock_time_s — union of intervals (real time the pipeline waited
                        on this model, with concurrent calls counted once)
  - max_concurrent   — peak overlap count
  - avg_call_s       — compute_time_s / count

Distinguishing these two is critical: when calls fan out concurrently (which
the workflow does for tree-building, OCR, and re-summarization), the sum
overcounts. Wall-clock is the metric that aligns with `total_runtime_s`.
"""


def empty_summary(document_id: str, run_id: str) -> dict:
    """Return the canonical empty summary shape used by the workflow as a base."""
    return {
        "document_id": document_id,
        "run_id": run_id,
        "total_runtime_s": 0.0,
        "stages": {},
        "counts": {},
        "llm_calls": {},
        "token_totals": {"input": 0, "output": 0, "total": 0},
    }


def _empty_bucket() -> dict:
    return {
        "count": 0,
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "intervals": [],  # list[tuple[float, float]] of (started_at, ended_at)
    }


def merge_call_record(
    summary: dict, model: str,
    *,
    started_at: float | None = None,
    ended_at: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    errored: bool = False,
) -> None:
    """In-place merge of one LLM/OCR call into an existing summary dict.

    On success: `started_at` and `ended_at` are appended as an interval.
    On error:   no interval is recorded; only `count` and `errors` are bumped.
    `finalize_summary` must be called once before the summary is reported.
    """
    bucket = summary["llm_calls"].setdefault(model, _empty_bucket())
    bucket["count"] += 1
    if errored:
        bucket["errors"] += 1
    elif started_at is not None and ended_at is not None:
        bucket["intervals"].append((float(started_at), float(ended_at)))

    bucket["input_tokens"] += int(input_tokens or 0)
    bucket["output_tokens"] += int(output_tokens or 0)

    totals = summary["token_totals"]
    totals["input"] += int(input_tokens or 0)
    totals["output"] += int(output_tokens or 0)
    totals["total"] = totals["input"] + totals["output"]


def _union_duration(intervals: list[tuple[float, float]]) -> float:
    """Total length of the union of (start, end) intervals.

    Concurrent (overlapping) calls contribute only the actual wall-clock
    elapsed, not the sum of their durations.
    """
    if not intervals:
        return 0.0
    sorted_iv = sorted(intervals)
    total = 0.0
    cur_start, cur_end = sorted_iv[0]
    for s, e in sorted_iv[1:]:
        if s > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = s, e
        else:
            cur_end = max(cur_end, e)
    total += cur_end - cur_start
    return total


def _peak_concurrency(intervals: list[tuple[float, float]]) -> int:
    """Peak number of simultaneously in-flight intervals (sweep-line)."""
    if not intervals:
        return 0
    # +1 at each start, -1 at each end; process starts before ends at ties
    # so a back-to-back call (end_i == start_j) doesn't register as overlap.
    events: list[tuple[float, int]] = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda ev: (ev[0], ev[1]))
    cur = 0
    peak = 0
    for _, delta in events:
        cur += delta
        if cur > peak:
            peak = cur
    return peak


def finalize_summary(summary: dict) -> None:
    """Collapse per-model `intervals` into final statistics. In-place."""
    for bucket in summary["llm_calls"].values():
        intervals: list[tuple[float, float]] = bucket.pop("intervals", [])
        compute_s = sum(e - s for s, e in intervals)
        wall_clock_s = _union_duration(intervals)
        bucket["compute_time_s"] = round(compute_s, 3)
        bucket["wall_clock_time_s"] = round(wall_clock_s, 3)
        bucket["avg_call_s"] = (
            round(compute_s / bucket["count"], 3) if bucket["count"] else 0.0
        )
        bucket["max_concurrent"] = _peak_concurrency(intervals)
