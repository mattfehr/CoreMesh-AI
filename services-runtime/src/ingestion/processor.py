"""Document ingestion processor — CoreMesh Step 1.1 (Project 14).

Orchestrates the full pipeline:
    loader → preprocess → dual OCR → variance / vision-fallback
    → structured extraction → invoice total validation

Public entry point: ``process_document(file_bytes, filename) → IngestResponse``
"""
import io
import logging
import time
from typing import List, Tuple

import numpy as np
from PIL import Image

from src.config import settings
from src.ingestion.extraction import extract_structured
from src.ingestion.ocr import run_dual_ocr, run_dual_ocr_with_fallback
from src.ingestion.preprocessing import page_to_grayscale, preprocess_pages
from src.ingestion.schemas import IngestResponse
from src.ingestion.validation import validate_invoice_totals
from src.ingestion.vision import extract_text_via_vision

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Document loaders
# ---------------------------------------------------------------------------

def _load_pdf(file_bytes: bytes) -> List[Image.Image]:
    from pdf2image import convert_from_bytes  # noqa: PLC0415

    return convert_from_bytes(
        file_bytes,
        dpi=300,
        poppler_path=settings.poppler_path or None,
    )


def _load_image(file_bytes: bytes) -> List[Image.Image]:
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    return [img]


def _load_pages(file_bytes: bytes, filename: str) -> List[Image.Image]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return _load_pdf(file_bytes)
    return _load_image(file_bytes)


# ---------------------------------------------------------------------------
# Per-page OCR with variance-driven fallback
# ---------------------------------------------------------------------------

def _ocr_page(page_np: np.ndarray, raw_gray: np.ndarray) -> Tuple[str, float, bool, str]:
    """Run dual OCR; escalate to vision LLM when variance exceeds the threshold.

    Returns:
        text          – final extracted text for the page
        variance      – dual-engine edit-distance variance (0.0–1.0)
        vision_used   – True when the vision LLM provided the text
        engine_label  – ``'tesseract'``, ``'easyocr'``, or ``'vision_llm'``
    """
    if settings.llm_available:
        best_text, variance, _conf, engine_label = run_dual_ocr(page_np)
    else:
        # Offline: compare preprocessed + raw OCR and pick by invoice keyword score.
        best_text, variance, _conf, engine_label = run_dual_ocr_with_fallback(
            page_np, raw_gray
        )
    vision_used = False

    if variance > settings.ocr_variance_threshold:
        if settings.llm_available:
            log.info(
                "OCR variance %.4f > threshold %.4f — invoking vision LLM",
                variance,
                settings.ocr_variance_threshold,
            )
            try:
                best_text = extract_text_via_vision(page_np)
                vision_used = True
                engine_label = "vision_llm"
            except Exception as exc:
                log.warning("Vision LLM fallback failed (%s) — using best OCR text", exc)
        else:
            log.info(
                "OCR variance %.4f > threshold (no API key) — using best-confidence OCR",
                variance,
            )

    return best_text, variance, vision_used, engine_label


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def process_document(file_bytes: bytes, filename: str) -> IngestResponse:
    """Process a document file (PDF or image) end-to-end.

    Args:
        file_bytes: Raw bytes of the uploaded file.
        filename:   Original filename; used to choose the loader (pdf vs image).

    Returns:
        A fully populated :class:`IngestResponse` including extraction,
        OCR metadata, and invoice-total validation result.
    """
    t_start = time.perf_counter()

    # 1. Load
    pages = _load_pages(file_bytes, filename)
    log.info("Loaded %d page(s) from %r", len(pages), filename)

    # 2. Preprocess
    preprocessed = preprocess_pages(pages)

    # 3. OCR all pages; track worst-case variance and dominant engine
    page_texts: list[str] = []
    max_variance = 0.0
    any_vision = False
    dominant_engine = "tesseract"

    for page, page_np in zip(pages, preprocessed):
        raw_gray = page_to_grayscale(page)
        text, var, vision_used, engine = _ocr_page(page_np, raw_gray)
        page_texts.append(text)
        if var > max_variance:
            max_variance = var
            dominant_engine = engine
        if vision_used:
            any_vision = True

    full_text = "\n\n".join(page_texts)
    log.debug("Combined OCR text (%d chars, first 400):\n%s", len(full_text), full_text[:400])

    # 4. Structured extraction
    extraction, llm_used = extract_structured(full_text)

    # 5. Validation
    validation = validate_invoice_totals(extraction)

    elapsed_ms = (time.perf_counter() - t_start) * 1_000

    return IngestResponse(
        extraction=extraction,
        ocr_engine_used=dominant_engine,
        ocr_variance=round(max_variance, 4),
        vision_fallback_used=any_vision,
        llm_extraction_used=llm_used,
        validation=validation,
        processing_time_ms=round(elapsed_ms, 2),
        page_count=len(pages),
    )
