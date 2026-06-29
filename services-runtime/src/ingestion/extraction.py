"""Structured invoice data extraction.

Two execution paths:
  - LLM path  (``OPENAI_API_KEY`` set): ``instructor`` + gpt-4o-mini enforces
    ``ExtractionTargetSchema`` via Pydantic.
  - Offline path (no API key): deterministic regex parser that expects the
    canonical invoice format produced by ``scripts/verify_ingestion.py``.
"""
import logging
import re
from typing import Any, Dict, List, Tuple

from src.config import settings
from src.ingestion.schemas import ExtractionTargetSchema

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a document data extraction assistant. "
    "Extract the requested fields strictly from the provided text. "
    "Do not infer, assume, or default values that are not present in the document. "
    "Return monetary amounts as plain floats with no currency symbols or commas."
)

_USER_TEMPLATE = (
    "Extract structured invoice data from the document text below.\n\n"
    "Document text:\n```\n{text}\n```"
)


# ---------------------------------------------------------------------------
# LLM extraction (instructor path)
# ---------------------------------------------------------------------------

def extract_with_llm(text: str) -> ExtractionTargetSchema:
    """Use instructor + gpt-4o-mini to produce a typed ``ExtractionTargetSchema``."""
    import instructor  # noqa: PLC0415
    from openai import OpenAI  # noqa: PLC0415

    client = instructor.from_openai(OpenAI(api_key=settings.openai_api_key))
    result: ExtractionTargetSchema = client.chat.completions.create(
        model=settings.openai_extraction_model,
        response_model=ExtractionTargetSchema,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(text=text)},
        ],
        max_tokens=2048,
    )
    log.info(
        "LLM extraction complete: vendor=%r  invoice_id=%r  items=%d",
        result.vendor_name,
        result.invoice_id,
        len(result.line_items),
    )
    return result


# ---------------------------------------------------------------------------
# Offline regex extraction
# ---------------------------------------------------------------------------

def _parse_float(raw: str) -> float:
    """Strip currency symbols and thousands separators then parse as float."""
    cleaned = re.sub(r"[,$£€\s_]", "", raw)
    cleaned = cleaned.replace(":", ".")
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    return float(cleaned)


def _normalize_ocr_text(text: str) -> str:
    """Collapse OCR artefacts that break strict regex matching."""
    text = re.sub(r"[^\x00-\x7F]", "", text)  # drop non-ASCII OCR noise
    text = re.sub(r"INVOICE[,_\s]+ID", "INVOICE_ID", text, flags=re.IGNORECASE)
    text = re.sub(r"LINE\s*ITEM\s*:", "LINEITEM:", text, flags=re.IGNORECASE)
    text = re.sub(r"d\s*e?\s*scription", "description", text, flags=re.IGNORECASE)
    text = re.sub(r"qty\s*=\s*l\b", "qty=1", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)\.+(\d)", r"\1.\2", text)
    text = re.sub(r"unit=([\d]+):([\d]{2})\b", r"unit=\1.\2", text, flags=re.IGNORECASE)
    text = re.sub(r"total=([\d]+):([\d]{2})\b", r"total=\1.\2", text, flags=re.IGNORECASE)
    text = re.sub(r"total=([\d]+)_\s*([\d.]+)", r"total=\1.\2", text, flags=re.IGNORECASE)
    text = re.sub(r"description\s*[-=]", "description=", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _extract_line_items(text: str) -> List[Dict[str, Any]]:
    """Parse LINEITEM rows from the canonical invoice format.

    Primary pattern (used by the synthetic invoice generator):
        LINEITEM: description=<desc> qty=<n> unit=<f> total=<f>

    Fallback pattern:
        <description> | <qty> | <unit> | <total>
    """
    primary = re.compile(
        r"LINEITEM\s*:?\s*"
        r"description\s*=\s*(?P<description>.+?)\s+"
        r"qty\s*=\s*(?P<qty>[l\d]+)\s+"
        r"unit\s*=\s*(?P<unit>[\d]+(?:[.:][\d]+)?)"
        r"(?:\s*\.?\s*|\s+[^\d=]*?)total\s*=\s*(?P<total>[\d]+(?:[.:][\d]+)?)",
        re.IGNORECASE,
    )
    items: List[Dict[str, Any]] = []
    for m in primary.finditer(text):
        qty_raw = m.group("qty").replace("l", "1").replace("L", "1")
        items.append(
            {
                "description": m.group("description").strip(),
                "quantity": int(qty_raw),
                "unit_price": _parse_float(m.group("unit")),
                "total": _parse_float(m.group("total")),
            }
        )
    if items:
        return items

    # Pipe-separated table fallback
    pipe_row = re.compile(
        r"^(?P<description>[^|]+)\|\s*(?P<qty>\d+)\s*\|\s*(?P<unit>[\d.,]+)\s*\|\s*(?P<total>[\d.,]+)",
        re.MULTILINE,
    )
    for m in pipe_row.finditer(text):
        desc = m.group("description").strip()
        if desc.lower() in {"description", "item", "lineitem", "line item", ""}:
            continue
        items.append(
            {
                "description": desc,
                "quantity": int(m.group("qty")),
                "unit_price": _parse_float(m.group("unit")),
                "total": _parse_float(m.group("total")),
            }
        )
    return items


def extract_with_regex(text: str) -> ExtractionTargetSchema:
    """Deterministic regex-based extraction.  Used when ``OPENAI_API_KEY`` is absent."""
    text = _normalize_ocr_text(text)

    vendor_m = re.search(r"VENDOR\s*:?\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    id_m = re.search(r"INVOICE_?ID\s*:?\s*([A-Z0-9\-]+)", text, re.IGNORECASE)
    tax_m = re.search(r"\bTAX\s*:?\s*\$?([\d,]+\.?\d*)", text, re.IGNORECASE)
    total_m = re.search(
        r"(?<!\w)TOTAL\s*:?\s*\$?([\d,]+\.?\d*)", text, re.IGNORECASE
    )

    vendor_name = vendor_m.group(1).strip() if vendor_m else "UNKNOWN"
    invoice_id = id_m.group(1).strip() if id_m else "UNKNOWN"
    calculated_tax = _parse_float(tax_m.group(1)) if tax_m else 0.0
    invoice_total = _parse_float(total_m.group(1)) if total_m else 0.0
    line_items = _extract_line_items(text)

    result = ExtractionTargetSchema(
        vendor_name=vendor_name,
        invoice_id=invoice_id,
        line_items=line_items,
        calculated_tax=calculated_tax,
        invoice_total=invoice_total,
    )
    log.info(
        "Regex extraction complete: vendor=%r  invoice_id=%r  items=%d",
        vendor_name,
        invoice_id,
        len(line_items),
    )
    return result


def score_parseable_text(text: str) -> int:
    """Score OCR text by how much structured invoice data regex can extract."""
    ext = extract_with_regex(text)
    score = 0
    if ext.vendor_name != "UNKNOWN":
        score += 10
    if ext.invoice_id != "UNKNOWN":
        score += 10
    score += len(ext.line_items) * 20
    if ext.calculated_tax > 0:
        score += 5
    if ext.invoice_total > 0:
        score += 5
    return score


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_structured(text: str) -> Tuple[ExtractionTargetSchema, bool]:
    """Return *(extraction, llm_used)*.

    Selects the LLM path when ``OPENAI_API_KEY`` is configured; falls back
    to the offline regex parser otherwise.
    """
    if settings.llm_available:
        return extract_with_llm(text), True
    return extract_with_regex(text), False
