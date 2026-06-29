"""CoreMesh AI — Python Runtime Service.

Entry point for uvicorn: ``src.main:app``
"""
import asyncio
import logging

import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile, status

from src.ingestion.processor import process_document
from src.ingestion.schemas import IngestResponse

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
log = structlog.get_logger(__name__)

app = FastAPI(
    title="CoreMesh AI Runtime",
    description=(
        "Python microservice providing document ingestion (multi-modal OCR), "
        "hybrid RAG, guardrailed text-to-SQL, and LangGraph agent orchestration."
    ),
    version="0.1.0",
)

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/webp",
}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"], summary="Service liveness check.")
async def health() -> dict:
    return {"status": "ok", "service": "coremesh-runtime"}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

@app.post(
    "/v1/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    tags=["ingestion"],
    summary="Ingest a document (PDF or image) and extract structured invoice data.",
    description=(
        "Accepts a PDF or raster image, runs dual-engine OCR (pytesseract + EasyOCR) "
        "with an optional GPT-4o vision fallback when OCR engines disagree, then uses "
        "``instructor`` + gpt-4o-mini to extract an ``ExtractionTargetSchema``. "
        "The response includes per-field extraction, OCR provenance metadata, "
        "and a line-item sum validation result."
    ),
)
async def ingest_document(file: UploadFile = File(...)) -> IngestResponse:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported content type: {file.content_type!r}. "
                f"Accepted: {sorted(_ALLOWED_CONTENT_TYPES)}"
            ),
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    log.info("ingest.start", filename=file.filename, size_bytes=len(file_bytes))

    try:
        result: IngestResponse = await asyncio.to_thread(
            process_document, file_bytes, file.filename or "upload"
        )
    except Exception as exc:
        log.error("ingest.error", filename=file.filename, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Document processing failed: {exc}",
        ) from exc

    log.info(
        "ingest.complete",
        filename=file.filename,
        ocr_engine=result.ocr_engine_used,
        variance=result.ocr_variance,
        validation_passed=result.validation.passed,
        elapsed_ms=result.processing_time_ms,
    )
    return result
