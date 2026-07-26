"""Cloud-API pricing for per-call cost estimation on run-report writes.

Hand-maintained USD-per-1M-tokens. Refresh when providers change posted
rates. Self-hosted (vLLM) models are DELIBERATELY absent — their cost is
amortized hardware, tracked separately via the CloudWatch/hardware path,
not per-token. Unknown models return `None`; callers render that as "—"
and suppress any run-total that included one, so we never fabricate a
figure.

Metric-summary labels for OCR are prefixed with `ocr:` (see
`prod/live/workflows/process_pdf.py::_enrich_ocr`); `_canonical` strips
that so the raw model id keys the table.
"""


# (input_usd_per_million, output_usd_per_million); refresh as posted rates change.
_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic — anthropic.com/pricing
    "claude-sonnet-4-5":            (3.00, 15.00),
    "claude-haiku-4-5":             (0.80,  4.00),
    "claude-opus-4-7":              (15.00, 75.00),
    # OpenAI — openai.com/api/pricing
    "gpt-4o":                       (2.50, 10.00),
    "gpt-4o-mini":                  (0.15,  0.60),
}


def _canonical(model: str) -> str:
    """Strip the `ocr:` metric-label prefix so the raw model id keys the table."""
    if model.startswith("ocr:"):
        return model[len("ocr:"):]
    return model


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int,
) -> float | None:
    """Return USD cost, or `None` when the model isn't in `_PRICING`
    (either self-hosted or a cloud model we haven't priced yet).

    Callers MUST render `None` as "—" and suppress any run-total that
    included one — do not silently coerce to 0.0.
    """
    rates = _PRICING.get(_canonical(model))
    if rates is None:
        return None
    in_rate, out_rate = rates
    return round(
        (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate,
        6,
    )
