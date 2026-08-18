"""Deterministic document-to-RAG chunk construction.

System role:
    Converts trusted, ordered OCR page text into stable retrieval chunks without
    exposing the raw text through the ingestion HTTP response.
Dependencies:
    Uses SHA-256 from the standard library and the shared RAG ``TextChunk``
    contract.
Side effects:
    None. Persistence and embedding are performed by ``HybridRetriever``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from src.rag.retrieval import TextChunk


def build_document_chunks(
    file_bytes: bytes,
    filename: str,
    page_texts: Sequence[str],
) -> tuple[str, list[TextChunk]]:
    """Return a content-addressed document ID and non-empty page chunks.

    Chunk IDs depend only on the uploaded bytes and one-based page number, so
    re-ingesting the same content updates the same Qdrant points. The filename
    remains useful provenance, but is deliberately not part of the identity.
    """

    document_id = hashlib.sha256(file_bytes).hexdigest()
    source = filename.strip() or "upload"
    chunks = [
        TextChunk(
            chunk_id=f"{document_id}:page:{page_number}",
            text=text.strip(),
            source=source,
            metadata={
                "document_id": document_id,
                "filename": source,
                "page_number": page_number,
            },
        )
        for page_number, text in enumerate(page_texts, start=1)
        if text.strip()
    ]
    return document_id, chunks


__all__ = ["build_document_chunks"]
