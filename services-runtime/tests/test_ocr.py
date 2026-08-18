"""Hermetic OCR provider-selection tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.ingestion import ocr  # noqa: E402


class _ExplodingReader:
    def readtext(self, _image):
        raise AssertionError("disabled EasyOCR reader must not be called")


def test_easyocr_disable_forces_tesseract_only_path(monkeypatch):
    monkeypatch.setattr(ocr.settings, "ocr_easyocr_enabled", False)
    monkeypatch.setattr(ocr, "_easyocr_reader", _ExplodingReader())
    monkeypatch.setattr(
        ocr,
        "run_tesseract",
        lambda _image: ("Acme invoice INV-100", 97.0),
    )

    result = ocr.run_dual_ocr(np.zeros((2, 2), dtype=np.uint8))

    assert result == ("Acme invoice INV-100", 0.0, 97.0, "tesseract")
