"""CoreMesh AI — Python Runtime Service.

System role:
    Owns the public runtime HTTP boundary. Liveness, document ingestion, and a
    minimal OpenAI-shaped chat completions path are mounted today; RAG, SQL,
    orchestration, and arbitration remain Python library APIs.
Dependencies:
    FastAPI handles HTTP/multipart contracts, ingestion owns blocking document
    work, chat completions may call OpenAI, and structlog emits request
    lifecycle metadata.
Side effects:
    Import configures structlog and constructs the FastAPI app. Requests read
    uploads into memory, run CPU/native work in a thread, and may call OpenAI.

Entry point for uvicorn: ``src.main:app``
"""
import asyncio
import logging

import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from pydantic import ValidationError

from src.chat.completions import ChatCompletionRequest, build_chat_completion
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
    """Return process liveness without contacting infrastructure providers."""
    return {"status": "ok", "service": "coremesh-runtime"}


# ---------------------------------------------------------------------------
# Chat completions (minimal OpenAI-compatible surface for gateway/CI)
# ---------------------------------------------------------------------------

@app.post(
    "/v1/chat/completions",
    tags=["chat"],
    summary="OpenAI-shaped chat completions for gateway regression traffic.",
    description=(
        "Accepts an OpenAI chat.completions JSON body. When COREMESH_CHAT_STUB "
        "is true or OPENAI_API_KEY is unset, returns a deterministic stub "
        "completion. Otherwise forwards to OpenAI."
    ),
)
async def chat_completions(payload: dict) -> dict:
    """Validate and answer one OpenAI-shaped chat completion request."""
    try:
        request = ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.errors(),
        ) from exc

    try:
        return await asyncio.to_thread(build_chat_completion, request)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        log.error("chat.completions.error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Chat completion failed: {exc}",
        ) from exc


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
    """Validate and process one in-memory PDF or raster upload.

    Unsupported declared media types return 415, empty bodies return 400, and
    loader/OCR/extraction failures are normalized to 422. The full upload is
    read before processing, so production callers need an outer size limit.
    """
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
        # OCR and image/model clients are synchronous and CPU/blocking. Moving
        # the pipeline off the event-loop thread preserves FastAPI concurrency.
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
