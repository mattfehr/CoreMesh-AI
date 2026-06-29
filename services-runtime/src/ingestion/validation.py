"""Invoice total validation.

Verifies that the sum of extracted line-item totals plus the calculated tax
matches the ``invoice_total`` field within a configurable monetary tolerance.
"""
import logging

from src.ingestion.schemas import ExtractionTargetSchema, ValidationResult

log = logging.getLogger(__name__)

# Default tolerance: $0.02 covers floating-point rounding and minor OCR artefacts.
_DEFAULT_TOLERANCE = 0.02


def validate_invoice_totals(
    extraction: ExtractionTargetSchema,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> ValidationResult:
    """Validate that ``sum(line_items[*].total) + calculated_tax ≈ invoice_total``.

    Each line item dict is expected to carry a ``"total"`` key.  Items with
    unparseable totals are skipped with a warning (non-fatal).

    Returns a :class:`ValidationResult` with ``passed=True`` when the absolute
    delta is within *tolerance*.
    """
    line_total = 0.0
    for item in extraction.line_items:
        raw = item.get("total", 0)
        try:
            line_total += float(raw)
        except (TypeError, ValueError):
            log.warning("Skipping unparseable line item total: %r", raw)

    computed_sum = round(line_total + extraction.calculated_tax, 6)
    delta = abs(computed_sum - extraction.invoice_total)
    passed = delta <= tolerance

    log.info(
        "Validation: computed=%.4f  invoice_total=%.4f  delta=%.4f  passed=%s",
        computed_sum,
        extraction.invoice_total,
        delta,
        passed,
    )

    return ValidationResult(
        passed=passed,
        computed_sum=round(computed_sum, 4),
        delta=round(delta, 4),
        tolerance=tolerance,
    )
