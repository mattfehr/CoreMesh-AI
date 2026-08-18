"""CoreMesh AI — Python Runtime Service.

System role:
    Owns the public runtime HTTP boundary: liveness, ingestion, minimal chat,
    restricted unified RAG/SQL/agent execution, and read-only forensic traces.
Dependencies:
    FastAPI handles HTTP/multipart contracts; agent and tracing packages own
    execution/forensics; ingestion and chat own their domain work; structlog
    emits request lifecycle metadata.
Side effects:
    Import configures structlog and constructs the FastAPI app. Requests read
    uploads, run blocking work in threads, call configured state/model systems,
    and create redacted forensic artifacts.

Entry point for uvicorn: ``src.main:app``
"""
import asyncio
import logging
import re
import sqlite3
import threading
from enum import Enum

import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.agents.orchestrator import (
    ExecutionRequestPayload,
    HybridRAGSearchTool,
    OrchestrationResult,
    OrchestratorDependencies,
    run_orchestration,
)
from src.chat.completions import ChatCompletionRequest, build_chat_completion
from src.ingestion.indexing import build_document_chunks
from src.ingestion.processor import ProcessedDocument, process_document_with_pages
from src.ingestion.schemas import IngestResponse, RAGIndexResult
from src.rag.retrieval import HybridRetriever
from src.tracing.forensics import (
    FailureCategory,
    FailureTrigger,
    ForensicTraceArtifact,
    ForensicTraceSummary,
    ForensicsTracer,
)

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


class ExecutionFeatureScope(str, Enum):
    """Browser-safe execution modes exposed by the unified runtime route."""

    RAG = "rag"
    TEXT_TO_SQL = "text_to_sql"
    AGENT_ORCHESTRATOR = "agent_orchestrator"


class ExecutionSessionContext(BaseModel):
    """Whitelisted session controls accepted from an untrusted HTTP client."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    rag_top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("session_id")
    @classmethod
    def normalize_session_id(cls, value: str | None) -> str | None:
        """Trim a provided session ID and reject whitespace-only values."""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("session_id must not be blank")
        return normalized


class ExecutionAPIRequest(BaseModel):
    """Public unified execution payload with a deliberately narrow context."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    feature_scope: ExecutionFeatureScope
    payload_query: str = Field(min_length=1, max_length=16_384)
    session_context: ExecutionSessionContext | None = None

    @field_validator("user_id", "payload_query")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        """Trim required text fields and reject whitespace-only values."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class TraceListResponse(BaseModel):
    """Paginated trace-registry response for the forensic explorer."""

    items: list[ForensicTraceSummary]
    total: int
    limit: int
    offset: int


_DEPENDENCY_LOCK = threading.Lock()
_RETRIEVER_LOCK = threading.Lock()


def get_rag_retriever() -> HybridRetriever:
    """Return the one retriever shared by ingestion and RAG executions."""

    retriever = getattr(app.state, "rag_retriever", None)
    if retriever is None:
        with _RETRIEVER_LOCK:
            retriever = getattr(app.state, "rag_retriever", None)
            if retriever is None:
                retriever = HybridRetriever()
                app.state.rag_retriever = retriever
    return retriever


def get_orchestrator_dependencies() -> OrchestratorDependencies:
    """Return one lazy application-scoped orchestration dependency graph."""

    dependencies = getattr(app.state, "orchestrator_dependencies", None)
    if dependencies is None:
        with _DEPENDENCY_LOCK:
            dependencies = getattr(app.state, "orchestrator_dependencies", None)
            if dependencies is None:
                dependencies = OrchestratorDependencies(
                    rag_tool=HybridRAGSearchTool(retriever=get_rag_retriever())
                )
                app.state.orchestrator_dependencies = dependencies
    return dependencies


def get_runtime_forensics() -> ForensicsTracer:
    """Return the tracer used by HTTP-triggered orchestrations."""

    return get_orchestrator_dependencies().forensics


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
# Unified execution
# ---------------------------------------------------------------------------

@app.post(
    "/v1/execute",
    response_model=OrchestrationResult,
    tags=["execution"],
    summary="Execute a RAG, text-to-SQL, or agent-orchestrator task.",
)
async def execute(
    payload: ExecutionAPIRequest,
    dependencies: OrchestratorDependencies = Depends(get_orchestrator_dependencies),
) -> OrchestrationResult:
    """Run synchronous orchestration off the event loop with HTTP-safe inputs."""

    context = (
        payload.session_context.model_dump(exclude_none=True)
        if payload.session_context is not None
        else None
    )
    request = ExecutionRequestPayload(
        user_id=payload.user_id,
        feature_scope=payload.feature_scope.value,
        payload_query=payload.payload_query,
        session_context=context,
    )
    try:
        return await asyncio.to_thread(run_orchestration, request, dependencies)
    except Exception as exc:
        log.error("execution.error", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="CoreMesh execution failed.",
        ) from exc


# ---------------------------------------------------------------------------
# Read-only forensic trace explorer
# ---------------------------------------------------------------------------

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@app.get(
    "/v1/traces",
    response_model=TraceListResponse,
    tags=["forensics"],
    summary="List redacted forensic trace summaries.",
)
async def list_traces(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    trace_status: str | None = Query(default=None, alias="status", max_length=64),
    trigger: FailureTrigger | None = Query(default=None),
    failure_category: FailureCategory | None = Query(default=None),
    forensics: ForensicsTracer = Depends(get_runtime_forensics),
) -> TraceListResponse:
    """Query the SQLite trace registry without returning artifact paths."""

    try:
        items, total = await asyncio.to_thread(
            forensics.list_traces,
            limit=limit,
            offset=offset,
            status=trace_status,
            trigger=trigger,
            failure_category=failure_category,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        log.error("forensics.list.error", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Forensic trace registry is unavailable.",
        ) from exc
    return TraceListResponse(items=items, total=total, limit=limit, offset=offset)


@app.get(
    "/v1/traces/{trace_id}",
    response_model=ForensicTraceArtifact,
    tags=["forensics"],
    summary="Read one redacted forensic trace artifact.",
)
async def get_trace(
    trace_id: str,
    forensics: ForensicsTracer = Depends(get_runtime_forensics),
) -> ForensicTraceArtifact:
    """Load one validated trace ID without allowing filesystem traversal."""

    if not _TRACE_ID_PATTERN.fullmatch(trace_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="trace_id must be 32 lowercase hexadecimal characters.",
        )
    try:
        return await asyncio.to_thread(forensics.get_trace, trace_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Forensic trace not found.",
        ) from exc
    except (OSError, UnicodeError, ValidationError) as exc:
        log.error("forensics.read.error", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Forensic trace artifact is unavailable.",
        ) from exc


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
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    tags=["ingestion"],
    summary="Ingest a document (PDF or image) and extract structured invoice data.",
    description=(
        "Accepts a PDF or raster image, runs dual-engine OCR (pytesseract + EasyOCR) "
        "with an optional GPT-4o vision fallback when OCR engines disagree, then uses "
        "``instructor`` + gpt-4o-mini to extract an ``ExtractionTargetSchema``. "
        "The response includes per-field extraction, OCR provenance metadata, "
        "and a line-item sum validation result. Set the multipart field "
        "``index_for_rag=true`` to persist non-empty page text for hybrid RAG."
    ),
)
async def ingest_document(
    file: UploadFile = File(...),
    index_for_rag: bool = Form(default=False),
) -> IngestResponse:
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
        processed: ProcessedDocument = await asyncio.to_thread(
            process_document_with_pages, file_bytes, file.filename or "upload"
        )
    except Exception as exc:
        log.error("ingest.error", filename=file.filename, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Document processing failed: {exc}",
        ) from exc

    result = processed.response
    if index_for_rag:
        document_id, chunks = build_document_chunks(
            file_bytes,
            file.filename or "upload",
            processed.page_texts,
        )
        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Document contains no indexable text.",
            )
        try:
            await asyncio.to_thread(get_rag_retriever().index_chunks, chunks)
        except Exception as exc:
            log.error(
                "ingest.index.error",
                filename=file.filename,
                document_id=document_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="RAG indexing dependencies are unavailable.",
            ) from exc
        result = result.model_copy(
            update={
                "rag_index": RAGIndexResult(
                    document_id=document_id,
                    chunk_count=len(chunks),
                )
            }
        )

    log.info(
        "ingest.complete",
        filename=file.filename,
        ocr_engine=result.ocr_engine_used,
        variance=result.ocr_variance,
        validation_passed=result.validation.passed,
        indexed_for_rag=index_for_rag,
        elapsed_ms=result.processing_time_ms,
    )
    return result
