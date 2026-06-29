"""Image preprocessing pipeline: deskew, grayscale, binarize, denoise.

Applied to every page before OCR to maximise text legibility.
"""
import logging
from typing import List

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PIL ↔ numpy conversions
# ---------------------------------------------------------------------------

def pil_to_bgr(image: Image.Image) -> np.ndarray:
    rgb = np.array(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def gray_to_pil(image: np.ndarray) -> Image.Image:
    return Image.fromarray(image)


# ---------------------------------------------------------------------------
# Individual pipeline stages
# ---------------------------------------------------------------------------

def _to_gray(image: np.ndarray) -> np.ndarray:
    if len(image.shape) == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def deskew(image: np.ndarray) -> np.ndarray:
    """Correct rotation angle using minAreaRect on foreground pixels.

    Returns the image unchanged when the detected angle is < 0.5° to avoid
    unnecessary re-sampling of already-level documents.
    """
    gray = _to_gray(image)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(binary > 0))
    if len(coords) < 10:
        return image

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    if abs(angle) < 0.5:
        return image

    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    log.debug("Deskewed %.2f°", angle)
    return rotated


def binarize(gray: np.ndarray) -> np.ndarray:
    """Otsu global threshold — produces high-contrast black/white output."""
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def denoise(binary: np.ndarray) -> np.ndarray:
    """Morphological opening removes isolated noise pixels (salt-and-pepper)."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)


# ---------------------------------------------------------------------------
# Public pipeline entry points
# ---------------------------------------------------------------------------

def preprocess_page(image: Image.Image) -> np.ndarray:
    """Full preprocessing pipeline for a single page.

    Accepts a PIL Image (any mode); returns a deskewed grayscale numpy array
    optimised for both pytesseract and EasyOCR input.

    Binarisation is intentionally skipped for the default path — Otsu +
    morphological opening often destroys clean rendered text (e.g. synthetic
    invoices).  ``binarize`` / ``denoise`` remain available for future
    adaptive use on low-quality scans.
    """
    bgr = pil_to_bgr(image)
    bgr = deskew(bgr)
    gray = _to_gray(bgr)
    # Mild contrast stretch helps faint scans without breaking crisp text.
    gray = cv2.convertScaleAbs(gray, alpha=1.15, beta=5)
    log.debug("Preprocessing complete: shape=%s", gray.shape)
    return gray


def page_to_grayscale(image: Image.Image) -> np.ndarray:
    """Convert a page to grayscale without deskew or contrast adjustment."""
    return _to_gray(pil_to_bgr(image))


def preprocess_pages(pages: List[Image.Image]) -> List[np.ndarray]:
    return [preprocess_page(p) for p in pages]
