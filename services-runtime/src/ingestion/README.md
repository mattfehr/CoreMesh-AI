# Document ingestion

This package converts an uploaded PDF or raster image into typed invoice data,
OCR provenance, and a deterministic arithmetic check. It is the only
intelligent-runtime capability currently exposed through FastAPI, and it is
also reused by the agent orchestrator's document specialist.

## Data flow

~~~text
bytes + filename
  -> PDF render or raster decode
  -> deskew / grayscale / contrast preprocessing
  -> Tesseract + optional EasyOCR on each page
  -> disagreement score and candidate selection
  -> optional OpenAI vision transcription above the threshold
  -> OpenAI/Instructor extraction or offline regex extraction
  -> invoice-total validation
  -> IngestResponse
~~~

FastAPI validates declared media type and empty content before calling this
package. The processor itself selects the loader from the filename suffix, so
trusted non-HTTP callers must validate file type independently.

## Module map

| Module | Responsibility |
| --- | --- |
| <code>processor.py</code> | Synchronous pipeline coordinator and public <code>process_document</code> entry point. |
| <code>preprocessing.py</code> | Pillow/OpenCV conversion, deskew, grayscale, contrast, and opt-in threshold/noise helpers. |
| <code>ocr.py</code> | Tesseract/EasyOCR execution, confidence normalization, disagreement scoring, and candidate choice. |
| <code>vision.py</code> | OpenAI vision transcription fallback for high-disagreement pages. |
| <code>extraction.py</code> | Schema-constrained online extraction and canonical-layout offline regex parsing. |
| <code>validation.py</code> | Line totals plus tax versus invoice-total comparison. |
| <code>schemas.py</code> | Pydantic extraction, validation, and HTTP response contracts. |

## Selection and fallback rules

When an OpenAI key is configured, each preprocessed page is compared across
Tesseract and EasyOCR. Disagreement above
<code>OCR_VARIANCE_THRESHOLD</code> attempts vision transcription; a vision
error keeps the best traditional OCR text. Structured extraction then uses the
configured OpenAI model and Instructor. Extraction errors propagate rather
than silently switching to regex.

Without an OpenAI key, OCR runs against both adjusted and raw-grayscale images
and ranks candidates by invoice-field coverage when they disagree. Extraction
uses a deterministic regex parser. That parser targets the canonical synthetic
invoice layout used by the verification script; UNKNOWN and zero values signal
missing fields rather than general extraction confidence.

EasyOCR is optional at runtime. Initialization or inference failure is logged
once/degraded to Tesseract. A missing Tesseract executable is a configuration
error and propagates.

## Dependencies and side effects

- Poppler renders PDFs at 300 DPI through <code>pdf2image</code>.
- Tesseract is a native executable configured by <code>TESSERACT_CMD</code> or
  the process path.
- EasyOCR runs in CPU mode and may download/cache weights on first use.
- Pillow, NumPy, and OpenCV allocate full page images and intermediate arrays.
- OpenAI-backed vision/extraction sends document content outside the process,
  adds latency, and can incur cost.
- The package logs metadata but does not write uploaded documents or results.

Uploads and all rendered pages are memory-resident. Put request-size and page
count limits in an authenticated outer boundary before production use.

## Public contracts

<code>process_document(file_bytes, filename)</code> returns
<code>IngestResponse</code>. Provenance reports the largest page disagreement,
whether any page used vision, whether LLM extraction ran, page count, and total
wall time. Validation passes when the sum of extracted line-item totals plus
tax differs from invoice total by no more than 0.02.

The validation is deliberately narrow: it does not recompute quantity times
unit price, validate vendors, or apply business rules.

## Verification

From <code>services-runtime</code>:

~~~powershell
python -m compileall -q src/ingestion
python scripts/verify_ingestion.py
~~~

The verification script generates a deterministic synthetic invoice and
exercises the offline pipeline. Native OCR dependencies must be installed; an
OpenAI key is not required for that path.

When extending the package, keep the online/offline mode boundary explicit,
preserve page order, update Pydantic/OpenAPI field descriptions with contract
changes, and add focused tests before accepting additional document types.
