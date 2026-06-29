"""Dual-engine OCR: pytesseract (primary) + EasyOCR (secondary).

Both engines run on every preprocessed page.  Their outputs are compared
using a normalised edit-distance variance metric.  If the engines agree
(variance ≤ threshold) the higher-confidence reading is used directly.
When they disagree significantly the caller may escalate to a vision LLM.
"""
import difflib
import logging
from typing import Optional, Tuple

import numpy as np
import pytesseract
from PIL import Image

from src.config import settings

log = logging.getLogger(__name__)

# Singleton EasyOCR reader; False acts as a "permanently unavailable" sentinel.
_easyocr_reader = None


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr  # noqa: PLC0415

            _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            log.info("EasyOCR reader initialised (CPU mode)")
        except Exception as exc:
            log.warning("EasyOCR unavailable: %s — tesseract-only path will be used", exc)
            _easyocr_reader = False
    return _easyocr_reader if _easyocr_reader is not False else None


def _configure_tesseract() -> None:
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd


# ---------------------------------------------------------------------------
# Engine runners
# ---------------------------------------------------------------------------

def run_tesseract(image: np.ndarray) -> Tuple[str, float]:
    """Return *(text, mean_confidence)* where confidence is 0–100.

    Raises ``pytesseract.TesseractNotFoundError`` if the binary is missing;
    callers should surface this as a configuration error.
    """
    _configure_tesseract()
    pil_img = Image.fromarray(image)
    data = pytesseract.image_to_data(pil_img, lang="eng", output_type=pytesseract.Output.DICT)
    confs = [
        int(c)
        for c in data["conf"]
        if str(c).lstrip("-").isdigit() and int(c) >= 0
    ]
    mean_conf = sum(confs) / len(confs) if confs else 0.0
    text = pytesseract.image_to_string(pil_img, lang="eng").strip()
    return text, mean_conf


def _easyocr_results_to_text(results: list) -> str:
    """Reconstruct line-ordered text from EasyOCR bounding-box results."""
    if not results:
        return ""
    # Sort by top-left Y then X to respect reading order
    sorted_results = sorted(results, key=lambda r: (r[0][0][1], r[0][0][0]))

    lines: list[list] = []
    current_y: Optional[float] = None
    line_gap_px = 15

    for bbox, text, _ in sorted_results:
        y = bbox[0][1]
        if current_y is None or abs(y - current_y) > line_gap_px:
            lines.append([])
            current_y = y
        lines[-1].append((bbox[0][0], text))

    return "\n".join(
        " ".join(t for _, t in sorted(line)) for line in lines
    )


def run_easyocr(image: np.ndarray) -> Tuple[Optional[str], float]:
    """Return *(text, mean_confidence 0–100)*, or *(None, 0.0)* if unavailable."""
    reader = _get_easyocr_reader()
    if reader is None:
        return None, 0.0
    try:
        results = reader.readtext(image)
        if not results:
            return "", 0.0
        mean_conf = sum(r[2] for r in results) / len(results)
        text = _easyocr_results_to_text(results)
        return text, mean_conf * 100  # normalise to the 0–100 scale
    except Exception as exc:
        log.warning("EasyOCR read failed: %s", exc)
        return None, 0.0


# ---------------------------------------------------------------------------
# Variance metric
# ---------------------------------------------------------------------------

def compute_variance(text1: str, text2: str) -> float:
    """Normalised edit-distance variance in [0, 1].

    0.0 = identical strings; 1.0 = completely different.
    Uses ``difflib.SequenceMatcher`` on the full text strings.
    """
    if not text1 and not text2:
        return 0.0
    if not text1 or not text2:
        return 1.0
    ratio = difflib.SequenceMatcher(None, text1, text2).ratio()
    return 1.0 - ratio


def score_invoice_text(text: str) -> int:
    """Heuristic score for how invoice-like an OCR output is."""
    from src.ingestion.extraction import score_parseable_text  # noqa: PLC0415

    return score_parseable_text(text)


def _pick_best_candidate(
    candidates: list[tuple[str, float, str]],
    variance: float,
) -> tuple[str, float, str]:
    """Select the best OCR candidate by confidence or invoice keyword score."""
    if variance <= settings.ocr_variance_threshold:
        # Engines agree — trust confidence.
        return max(candidates, key=lambda c: c[1])

    # Engines disagree — prefer invoice keyword coverage over raw confidence.
    return max(candidates, key=lambda c: (score_invoice_text(c[0]), c[1]))


# ---------------------------------------------------------------------------
# Combined dual-engine entry point
# ---------------------------------------------------------------------------

def run_dual_ocr(page_image: np.ndarray) -> Tuple[str, float, float, str]:
    """Run both OCR engines and return the higher-confidence result.

    Returns:
        best_text     – text from the winning engine
        variance      – normalised variance between the two engine outputs
        best_conf     – confidence (0–100) of the winning engine
        engine_label  – ``'tesseract'`` or ``'easyocr'``

    When EasyOCR is unavailable, variance is ``0.0`` and the tesseract text
    is returned (no comparison is possible).
    """
    tess_text, tess_conf = run_tesseract(page_image)
    easy_text, easy_conf = run_easyocr(page_image)

    if easy_text is None:
        # Single engine — no variance to compute
        return tess_text, 0.0, tess_conf, "tesseract"

    variance = compute_variance(tess_text, easy_text)
    log.debug(
        "OCR variance=%.4f  tesseract_conf=%.1f  easyocr_conf=%.1f",
        variance,
        tess_conf,
        easy_conf,
    )

    best_text, best_conf, engine_label = _pick_best_candidate(
        [
            (tess_text, tess_conf, "tesseract"),
            (easy_text, easy_conf, "easyocr"),
        ],
        variance,
    )
    return best_text, variance, best_conf, engine_label


def run_dual_ocr_with_fallback(page_image: np.ndarray, raw_gray: np.ndarray) -> Tuple[str, float, float, str]:
    """Run dual OCR on preprocessed and raw images; pick the best result.

    When engines disagree (high variance), evaluates all candidate outputs
    from both images and selects the one with the strongest invoice keyword
    coverage — critical for offline mode without a vision LLM.
    """
    tess_pre, tess_conf_pre = run_tesseract(page_image)
    easy_pre, easy_conf_pre = run_easyocr(page_image)
    tess_raw, tess_conf_raw = run_tesseract(raw_gray)
    easy_raw, easy_conf_raw = run_easyocr(raw_gray)

    candidates: list[tuple[str, float, str]] = [
        (tess_pre, tess_conf_pre, "tesseract"),
    ]
    if easy_pre is not None:
        candidates.append((easy_pre, easy_conf_pre, "easyocr"))
    candidates.append((tess_raw, tess_conf_raw, "tesseract_raw"))
    if easy_raw is not None:
        candidates.append((easy_raw, easy_conf_raw, "easyocr_raw"))

    # Variance across primary preprocessed engine pair.
    if easy_pre is not None:
        variance = compute_variance(tess_pre, easy_pre)
    else:
        variance = compute_variance(tess_pre, tess_raw)

    best_text, best_conf, engine_label = _pick_best_candidate(candidates, variance)
    log.debug(
        "OCR fallback pick: engine=%s variance=%.4f score=%d",
        engine_label,
        variance,
        score_invoice_text(best_text),
    )
    return best_text, variance, best_conf, engine_label.replace("_raw", "")
