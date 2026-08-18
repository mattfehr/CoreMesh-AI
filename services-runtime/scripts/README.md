# Runtime verification scripts

<code>verify_ingestion.py</code> is an in-process end-to-end smoke test rather
than a pytest unit test. It:

1. checks Tesseract/EasyOCR/OpenAI mode availability;
2. deterministically renders a noisy synthetic invoice;
3. overwrites <code>fixtures/synthetic_invoice.png</code>;
4. posts it to FastAPI through <code>TestClient</code>;
5. asserts known fields, totals, provenance, and validation.

Run from <code>services-runtime</code>:

~~~powershell
python scripts/verify_ingestion.py
~~~

Tesseract is required for a useful offline run. EasyOCR can degrade to
Tesseract; set <code>OCR_EASYOCR_ENABLED=false</code> to prevent its
initialization and model download entirely. If <code>OPENAI_API_KEY</code> is
nonempty, normal online paths may transmit the generated invoice and incur
cost; keep it blank when specifically verifying offline extraction.

The script returns nonzero when assertions fail and prints manual diagnostics.
It is intentionally allowed to regenerate the tracked fixture.
