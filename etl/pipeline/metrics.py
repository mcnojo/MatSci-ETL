"""Pipeline run metrics via contextvars.

Activities set a PipelineMetrics instance at entry. LLM helpers and enrichment
code record call timings into it without global state or explicit threading.

Usage in activities:
    metrics = PipelineMetrics(document_id, run_id)
    set_current_metrics(metrics)
    ...  # LLM calls automatically record into metrics
    summary = metrics.summary()

Usage in LLM/OCR call sites:
    m = get_current_metrics()
    if m:
        m.record_llm_call(model, duration, ...)
"""

import contextvars
import logging
import time
from contextlib import contextmanager

log = logging.getLogger("pipeline.metrics")

_current_metrics: contextvars.ContextVar[PipelineMetrics | None] = contextvars.ContextVar(
    "pipeline_metrics", default=None,
)


def get_current_metrics() -> PipelineMetrics | None:
    return _current_metrics.get()


def set_current_metrics(m: PipelineMetrics | None) -> contextvars.Token:
    return _current_metrics.set(m)


class PipelineMetrics:
    """Collects LLM call stats and element counts for a single pipeline run."""

    def __init__(self, document_id: str, run_id: str):
        self.document_id = document_id
        self.run_id = run_id
        self._t0 = time.perf_counter()
        self._llm_calls: dict[str, dict] = {}
        self._counts: dict[str, int] = {}
        self._stages: dict[str, dict] = {}

    def record_llm_call(
        self,
        model: str,
        duration_s: float,
        error: bool = False,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        bucket = self._llm_calls.setdefault(
            model,
            {"count": 0, "total_s": 0.0, "errors": 0, "input_tokens": 0, "output_tokens": 0},
        )
        bucket["count"] += 1
        bucket["total_s"] += duration_s
        bucket["input_tokens"] += int(input_tokens or 0)
        bucket["output_tokens"] += int(output_tokens or 0)
        if error:
            bucket["errors"] += 1

    def record_counts(self, **counts: int):
        self._counts.update(counts)

    @contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._stages[name] = {"duration_s": round(time.perf_counter() - t0, 3)}

    def summary(self) -> dict:
        total_in = total_out = 0
        per_model = {}
        for model, d in self._llm_calls.items():
            in_t = d["input_tokens"]
            out_t = d["output_tokens"]
            total_in += in_t
            total_out += out_t
            per_model[model] = {
                "count": d["count"],
                "total_s": round(d["total_s"], 3),
                "avg_s": round(d["total_s"] / d["count"], 3) if d["count"] else 0,
                "errors": d["errors"],
                "input_tokens": in_t,
                "output_tokens": out_t,
            }
        return {
            "document_id": self.document_id,
            "run_id": self.run_id,
            "total_runtime_s": round(time.perf_counter() - self._t0, 3),
            "stages": self._stages,
            "counts": self._counts,
            "llm_calls": per_model,
            "token_totals": {
                "input": total_in,
                "output": total_out,
                "total": total_in + total_out,
            },
        }
