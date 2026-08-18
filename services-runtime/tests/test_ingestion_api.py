"""Opt-in ingestion-to-RAG HTTP contract tests.

System role:
    Protects extraction-only compatibility, content-addressed page indexing,
    shared retriever wiring, and indexing-specific HTTP failures.
Dependencies:
    FastAPI TestClient with injected processor/retriever fakes.
Side effects:
    None; OCR, Qdrant, and model providers are not contacted.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import src.main as runtime_main  # noqa: E402
from src.ingestion.processor import ProcessedDocument  # noqa: E402
from src.ingestion.schemas import (  # noqa: E402
    ExtractionTargetSchema,
    IngestResponse,
    ValidationResult,
)


client = TestClient(runtime_main.app)


class RecordingRetriever:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.index_calls = []

    def index_chunks(self, chunks) -> None:
        self.index_calls.append(list(chunks))
        if self.error is not None:
            raise self.error

    def search(self, _query, top_k=5):
        return []


def _processed(*page_texts: str) -> ProcessedDocument:
    return ProcessedDocument(
        response=IngestResponse(
            extraction=ExtractionTargetSchema(
                vendor_name="Acme Corp",
                invoice_id="INV-100",
                line_items=[{"description": "Software License", "total": 100.0}],
                calculated_tax=8.0,
                invoice_total=108.0,
            ),
            ocr_engine_used="tesseract",
            ocr_variance=0.0,
            vision_fallback_used=False,
            llm_extraction_used=False,
            validation=ValidationResult(
                passed=True,
                computed_sum=108.0,
                delta=0.0,
                tolerance=0.02,
            ),
            processing_time_ms=12.5,
            page_count=len(page_texts),
        ),
        page_texts=tuple(page_texts),
    )


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    attributes = ("rag_retriever", "orchestrator_dependencies")
    saved = {
        name: getattr(runtime_main.app.state, name)
        for name in attributes
        if hasattr(runtime_main.app.state, name)
    }
    for name in attributes:
        if hasattr(runtime_main.app.state, name):
            delattr(runtime_main.app.state, name)
    yield
    for name in attributes:
        if hasattr(runtime_main.app.state, name):
            delattr(runtime_main.app.state, name)
    for name, value in saved.items():
        setattr(runtime_main.app.state, name, value)


def test_ingest_opt_out_preserves_extraction_only_response(monkeypatch):
    monkeypatch.setattr(
        runtime_main,
        "process_document_with_pages",
        lambda _body, _filename: _processed("invoice text"),
    )
    monkeypatch.setattr(
        runtime_main,
        "get_rag_retriever",
        lambda: pytest.fail("opt-out ingestion must not construct the RAG retriever"),
    )

    response = client.post(
        "/v1/ingest",
        files={"file": ("invoice.png", b"same-document", "image/png")},
    )

    assert response.status_code == 200
    assert "rag_index" not in response.json()


def test_ingest_indexes_nonempty_pages_with_deterministic_identity(monkeypatch):
    retriever = RecordingRetriever()
    runtime_main.app.state.rag_retriever = retriever
    monkeypatch.setattr(
        runtime_main,
        "process_document_with_pages",
        lambda _body, _filename: _processed(
            "Acme Corp invoice INV-100",
            "   ",
            "Software License total 100.00 tax 8.00",
        ),
    )
    file_bytes = b"same-document"

    response = client.post(
        "/v1/ingest",
        files={"file": ("invoice.png", file_bytes, "image/png")},
        data={"index_for_rag": "true"},
    )

    assert response.status_code == 200
    document_id = hashlib.sha256(file_bytes).hexdigest()
    assert response.json()["rag_index"] == {
        "document_id": document_id,
        "chunk_count": 2,
    }
    assert len(retriever.index_calls) == 1
    chunks = retriever.index_calls[0]
    assert [chunk.chunk_id for chunk in chunks] == [
        f"{document_id}:page:1",
        f"{document_id}:page:3",
    ]
    assert [chunk.metadata["page_number"] for chunk in chunks] == [1, 3]
    assert all(chunk.metadata["document_id"] == document_id for chunk in chunks)

    dependencies = runtime_main.get_orchestrator_dependencies()
    assert dependencies.rag_tool.retriever is retriever


def test_ingest_indexing_rejects_document_without_text(monkeypatch):
    retriever = RecordingRetriever()
    runtime_main.app.state.rag_retriever = retriever
    monkeypatch.setattr(
        runtime_main,
        "process_document_with_pages",
        lambda _body, _filename: _processed("", "  \n "),
    )

    response = client.post(
        "/v1/ingest",
        files={"file": ("blank.png", b"blank", "image/png")},
        data={"index_for_rag": "true"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Document contains no indexable text."}
    assert retriever.index_calls == []


def test_ingest_indexing_dependency_failure_is_sanitized_503(monkeypatch):
    runtime_main.app.state.rag_retriever = RecordingRetriever(
        RuntimeError("PRIVATE_QDRANT_DETAILS")
    )
    monkeypatch.setattr(
        runtime_main,
        "process_document_with_pages",
        lambda _body, _filename: _processed("Acme invoice"),
    )

    response = client.post(
        "/v1/ingest",
        files={"file": ("invoice.png", b"document", "image/png")},
        data={"index_for_rag": "true"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "RAG indexing dependencies are unavailable."
    }
    assert "PRIVATE_QDRANT_DETAILS" not in response.text
