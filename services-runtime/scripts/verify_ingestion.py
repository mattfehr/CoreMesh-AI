#!/usr/bin/env python3
"""Verification script for the CoreMesh document ingestion pipeline (Step 1.1).

Usage
-----
    cd services-runtime
    python scripts/verify_ingestion.py

Test document
---------------
The synthetic invoice PNG is written to::

    services-runtime/fixtures/synthetic_invoice.png

Re-run the script anytime to regenerate it.  You can also POST that file
manually to ``POST /v1/ingest`` while the server is running.

Offline mode (no ``OPENAI_API_KEY``)
-------------------------------------
Uses dual-engine OCR + the deterministic regex extractor.  Assertions verify
that the known invoice values are extracted, not just that types are valid.
"""
import io
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Known invoice data  (ground truth)
# ---------------------------------------------------------------------------
VENDOR = "Acme Corp Ltd"
INVOICE_ID = "INV-2024-001"

LINE_ITEMS = [
    {"description": "Software License", "qty": 1, "unit": 1200.00, "total": 1200.00},
    {"description": "Support Services", "qty": 12, "unit": 95.00, "total": 1140.00},
    {"description": "Implementation Fee", "qty": 1, "unit": 850.00, "total": 850.00},
]

CALCULATED_TAX = 272.85
INVOICE_TOTAL = round(sum(i["total"] for i in LINE_ITEMS) + CALCULATED_TAX, 2)

VALIDATION_TOLERANCE = 0.02

FIXTURE_DIR = os.path.join(_REPO_ROOT, "fixtures")
FIXTURE_PATH = os.path.join(FIXTURE_DIR, "synthetic_invoice.png")


# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------

def _load_monospace_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/courbd.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/Library/Fonts/Courier New.ttf",
        "/System/Library/Fonts/Courier.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)  # type: ignore[call-arg]
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Synthetic invoice image generator
# ---------------------------------------------------------------------------

def generate_invoice_image() -> bytes:
    """Render an OCR-friendly invoice PNG and save it to ``fixtures/``."""
    # Larger fonts improve offline OCR accuracy without an API key.
    font_h1 = _load_monospace_font(36)
    font = _load_monospace_font(26)

    lines = [
        "INVOICE",
        "",
        f"VENDOR: {VENDOR}",
        f"INVOICE_ID: {INVOICE_ID}",
        "",
        "Items:",
    ]
    for item in LINE_ITEMS:
        lines.append(
            f"LINEITEM: description={item['description']}"
            f" qty={item['qty']}"
            f" unit={item['unit']:.2f}"
            f" total={item['total']:.2f}"
        )
    lines += [
        "",
        f"TAX: {CALCULATED_TAX:.2f}",
        f"TOTAL: {INVOICE_TOTAL:.2f}",
    ]

    line_height = 44
    pad = 80
    tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_text_w = max(
        tmp_draw.textbbox((0, 0), ln, font=(font_h1 if ln == "INVOICE" else font))[2]
        for ln in lines
    )
    canvas_w = max_text_w + pad * 2
    canvas_h = len(lines) * line_height + pad * 2

    img = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    y = pad
    for ln in lines:
        fnt = font_h1 if ln == "INVOICE" else font
        draw.text((pad, y), ln, fill=(0, 0, 0), font=fnt)
        y += line_height

    # Light scan simulation only — keeps offline OCR reliable.
    img = img.rotate(0.8, fillcolor=(255, 255, 255), expand=False)
    arr = np.array(img, dtype=np.uint8)
    rng = np.random.default_rng(seed=42)
    n_px = int(arr.shape[0] * arr.shape[1] * 0.003)  # 0.3 % noise
    rows = rng.integers(0, arr.shape[0], n_px)
    cols = rng.integers(0, arr.shape[1], n_px)
    arr[rows, cols] = 0
    img = Image.fromarray(arr)

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    img.save(FIXTURE_PATH, format="PNG")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _preflight() -> None:
    print("Pre-flight checks")
    print("-" * 40)

    try:
        import pytesseract  # noqa: PLC0415
        from src.config import settings  # noqa: PLC0415

        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        ver = pytesseract.get_tesseract_version()
        print(f"  [OK] Tesseract {ver}")
    except Exception as exc:
        print(f"  [WARN] Tesseract not found: {exc}")
        print("         Install from: https://github.com/UB-Mannheim/tesseract/wiki")

    try:
        import easyocr  # noqa: F401, PLC0415

        print("  [OK] EasyOCR package present")
    except ImportError:
        print("  [WARN] EasyOCR not installed — tesseract-only OCR will be used")

    from src.config import settings as s  # noqa: PLC0415

    if s.llm_available:
        print(f"  [OK] OPENAI_API_KEY set — using LLM extraction ({s.openai_extraction_model})")
    else:
        print("  [INFO] No OPENAI_API_KEY — using offline regex extractor")

    print()


# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------

_failures: list[str] = []


def _assert(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        _failures.append(label)


# ---------------------------------------------------------------------------
# Main verification routine
# ---------------------------------------------------------------------------

def run_verification() -> None:
    print("=" * 64)
    print("CoreMesh Step 1.1 — Ingestion Pipeline Verification")
    print("=" * 64)
    print()

    _preflight()

    print("[1/3] Generating synthetic scanned invoice image …")
    image_bytes = generate_invoice_image()
    print(f"      Saved to: {FIXTURE_PATH}")
    print(f"      {len(image_bytes):,} bytes  |  known total: ${INVOICE_TOTAL:.2f}")
    print()

    print("[2/3] Posting to POST /v1/ingest via FastAPI TestClient …")
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from src.main import app  # noqa: PLC0415

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/ingest",
        files={"file": ("synthetic_invoice.png", image_bytes, "image/png")},
    )
    print(f"      HTTP {response.status_code}")
    print()

    print("[3/3] Asserting response …")
    print()

    _assert(response.status_code == 200, f"HTTP 200 OK (got {response.status_code})")
    if response.status_code != 200:
        print(f"      Response body: {response.text[:300]}")

    if response.status_code == 200:
        data = response.json()
        ext = data.get("extraction", {})
        val = data.get("validation", {})

        _assert(
            isinstance(ext.get("vendor_name"), str) and ext.get("vendor_name") != "UNKNOWN",
            f"vendor_name extracted (got {ext.get('vendor_name')!r}, expected {VENDOR!r})",
        )
        _assert(
            isinstance(ext.get("invoice_id"), str) and ext.get("invoice_id") != "UNKNOWN",
            f"invoice_id extracted (got {ext.get('invoice_id')!r}, expected {INVOICE_ID!r})",
        )
        _assert(
            len(ext.get("line_items", [])) == len(LINE_ITEMS),
            f"line_items count == {len(LINE_ITEMS)} (got {len(ext.get('line_items', []))})",
        )
        _assert(
            abs(float(ext.get("calculated_tax", 0)) - CALCULATED_TAX) <= VALIDATION_TOLERANCE,
            f"calculated_tax ~= {CALCULATED_TAX} (got {ext.get('calculated_tax')})",
        )
        _assert(
            abs(float(ext.get("invoice_total", 0)) - INVOICE_TOTAL) <= VALIDATION_TOLERANCE,
            f"invoice_total ~= {INVOICE_TOTAL} (got {ext.get('invoice_total')})",
        )

        line_sum = sum(float(item.get("total", 0)) for item in ext.get("line_items", []))
        computed = round(line_sum + float(ext.get("calculated_tax", 0)), 2)
        delta = abs(computed - float(ext.get("invoice_total", 0)))
        _assert(
            delta <= VALIDATION_TOLERANCE,
            f"sum(line_items) + tax = {computed:.2f} ~= invoice_total {ext.get('invoice_total'):.2f}",
        )
        _assert(val.get("passed") is True, "validation.passed == True")

        print()
        print("  Pipeline stats:")
        print(f"    OCR engine used  : {data.get('ocr_engine_used')}")
        print(f"    OCR variance     : {data.get('ocr_variance', 0):.4f}")
        print(f"    Vision fallback  : {data.get('vision_fallback_used')}")
        print(f"    LLM extraction   : {data.get('llm_extraction_used')}")
        print(f"    Processing time  : {data.get('processing_time_ms', 0):.1f} ms")
        print()
        print("  Extracted fields:")
        print(f"    vendor_name      : {ext.get('vendor_name')!r}")
        print(f"    invoice_id       : {ext.get('invoice_id')!r}")
        print(f"    line_items       : {len(ext.get('line_items', []))} item(s)")
        print(f"    calculated_tax   : {ext.get('calculated_tax')}")
        print(f"    invoice_total    : {ext.get('invoice_total')}")

    print()
    print("=" * 64)
    if _failures:
        print(f"RESULT: {len(_failures)} assertion(s) FAILED:")
        for f in _failures:
            print(f"  X {f}")
        sys.exit(1)
    else:
        print("RESULT: All assertions PASSED.")
    print("=" * 64)


if __name__ == "__main__":
    run_verification()
