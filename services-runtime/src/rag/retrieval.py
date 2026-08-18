"""Hybrid dense/sparse retrieval for CoreMesh Step 1.2.

System role:
    Produces source-marked evidence for Python callers and the supervisor's RAG
    specialist by combining semantic and lexical recall.
Dependencies:
    Default adapters use OpenAI embeddings, persistent Qdrant vectors,
    process-local BM25, and a sentence-transformers cross-encoder.
Side effects:
    Indexing calls OpenAI and writes Qdrant while retaining a local sparse
    corpus; searching calls OpenAI/Qdrant and can load/download reranker weights.

The module keeps the production path wired to Qdrant, OpenAI embeddings, and a
cross-encoder reranker while allowing tests to inject lightweight fakes.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

from pydantic import BaseModel, Field

from src.config import settings
from src.tracing.forensics import SpanCategory, forensic_span

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:[.\-/:][A-Za-z0-9_]+)*")


class TextChunk(BaseModel):
    """Indexable source fragment with a caller-stable identifier."""
    chunk_id: str
    text: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """Ranked evidence plus dense/sparse provenance and citation marker."""
    chunk_id: str
    text: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    reference_marker: str
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rrf_score: float
    rerank_score: float


@dataclass(frozen=True)
class SearchHit:
    """Internal adapter-neutral chunk and raw index score."""
    chunk: TextChunk
    score: float


class EmbeddingProvider(Protocol):
    """Batch text-to-vector provider contract."""
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class DenseIndex(Protocol):
    """Persistent dense-vector indexing and query contract."""
    def index_chunks(self, chunks: Sequence[TextChunk], vectors: Sequence[Sequence[float]]) -> None:
        ...

    def search(self, query_vector: Sequence[float], limit: int) -> list[SearchHit]:
        ...

    def load_chunks(self) -> list[TextChunk]:
        ...


class Reranker(Protocol):
    """Final query/chunk scoring contract."""
    def score(self, query: str, chunks: Sequence[TextChunk]) -> list[float]:
        ...


def tokenize(text: str) -> list[str]:
    """Tokenize prose and technical identifiers without shredding code symbols.

    Term frequency is preserved (no global dedup) so BM25 scoring stays
    meaningful. Compound identifiers such as ``CircuitBreakerState.OPEN`` are
    emitted both whole and as their separator-split parts so exact-symbol and
    sub-token lookups both match, but each part is only added once per
    occurrence to avoid inflating frequencies.
    """
    tokens: list[str] = []

    for match in TOKEN_PATTERN.finditer(text):
        token = match.group(0).lower()
        if not token:
            continue
        tokens.append(token)

        parts = re.split(r"[.\-/:]", token)
        if len(parts) > 1:
            for part in parts:
                if part and part != token:
                    tokens.append(part)

    return tokens


def technical_tokens(text: str) -> set[str]:
    """Return exact compound/code identifiers eligible for priority ranking."""
    tokens: set[str] = set()
    for token in TOKEN_PATTERN.findall(text):
        if any(sep in token for sep in "._-/:") or re.search(r"[a-z][A-Z]", token):
            tokens.add(token.lower())
    return tokens


def stable_point_id(chunk_id: str) -> str:
    """Map a caller chunk ID to a deterministic Qdrant-compatible UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"coremesh:rag:{chunk_id}"))


class OpenAIEmbeddingProvider:
    """Lazy OpenAI embedding adapter; requires a key only when used."""
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or settings.openai_embedding_model
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for dense embedding retrieval.")
        if self._client is None:
            from openai import OpenAI  # noqa: PLC0415

            self._client = OpenAI(api_key=self.api_key)
        return self._client

    @forensic_span("coremesh.model.openai.embedding", SpanCategory.MODEL)
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in one provider request, preserving input order."""
        if not texts:
            return []
        response = self.client.embeddings.create(model=self.model, input=list(texts))
        return [item.embedding for item in response.data]


class HashEmbeddingProvider:
    """Credential-free, normalized feature hashing for hermetic validation.

    This adapter intentionally provides deterministic lexical similarity, not
    a production semantic model. Production keeps the OpenAI provider default.
    """

    def __init__(self, vector_size: int | None = None) -> None:
        self.vector_size = (
            settings.qdrant_vector_size if vector_size is None else vector_size
        )
        if self.vector_size <= 0:
            raise ValueError("Hash embedding vector size must be positive.")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.vector_size
        counts = Counter(tokenize(text))
        for token, count in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in (0, 8):
                index = int.from_bytes(digest[offset : offset + 8], "big") % self.vector_size
                sign = 1.0 if digest[offset + 16] & 1 else -1.0
                vector[index] += sign * float(count)

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # Preserve Qdrant's non-empty vector contract for blank text while
            # keeping the result deterministic and unit-normalized.
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:8], "big") % self.vector_size] = 1.0
            return vector
        return [value / norm for value in vector]


class QdrantDenseIndex:
    """Lazy Qdrant collection adapter for persistent dense chunks."""
    def __init__(
        self,
        collection_name: str | None = None,
        url: str | None = None,
        vector_size: int | None = None,
    ) -> None:
        self.collection_name = collection_name or settings.qdrant_collection
        self.url = url or settings.qdrant_url
        self.vector_size = vector_size or settings.qdrant_vector_size
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from qdrant_client import QdrantClient  # noqa: PLC0415

            self._client = QdrantClient(url=self.url)
        return self._client

    @forensic_span("coremesh.db.qdrant.ensure_collection", SpanCategory.DATABASE)
    def ensure_collection(self) -> None:
        """Create the configured cosine collection when it does not exist."""
        from qdrant_client import models  # noqa: PLC0415

        if self.client.collection_exists(self.collection_name):
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(
                size=self.vector_size,
                distance=models.Distance.COSINE,
            ),
        )

    @forensic_span("coremesh.db.qdrant.upsert", SpanCategory.DATABASE)
    def index_chunks(self, chunks: Sequence[TextChunk], vectors: Sequence[Sequence[float]]) -> None:
        """Upsert aligned chunks/vectors under deterministic point IDs."""
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length.")
        if not chunks:
            return

        from qdrant_client import models  # noqa: PLC0415

        self.ensure_collection()
        points = [
            models.PointStruct(
                id=stable_point_id(chunk.chunk_id),
                vector=list(vector),
                payload={
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "source": chunk.source,
                    "metadata": chunk.metadata,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )

    @forensic_span("coremesh.db.qdrant.query", SpanCategory.DATABASE)
    def search(self, query_vector: Sequence[float], limit: int) -> list[SearchHit]:
        """Query nearest persistent points and normalize their payloads."""
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=list(query_vector),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [_hit_from_qdrant_point(point) for point in response.points]

    @forensic_span("coremesh.db.qdrant.scroll", SpanCategory.DATABASE)
    def load_chunks(self) -> list[TextChunk]:
        """Load all persisted payloads for one-time sparse-index rebuilding."""

        if not self.client.collection_exists(self.collection_name):
            return []

        chunks: list[TextChunk] = []
        offset: Any | None = None
        while True:
            points, next_offset = self.client.scroll(
                collection_name=self.collection_name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            chunks.extend(_chunk_from_qdrant_payload(point.payload or {}) for point in points)
            if next_offset is None:
                break
            offset = next_offset
        return sorted(chunks, key=lambda chunk: chunk.chunk_id)


class BM25SparseIndex:
    """Process-local lexical index, with a small fallback implementation."""
    def __init__(self) -> None:
        self._chunks: list[TextChunk] = []
        self._tokenized: list[list[str]] = []
        self._bm25: Any | None = None
        self._fallback_bm25: _SimpleBM25 | None = None
        self._lock = threading.RLock()

    @property
    def has_chunks(self) -> bool:
        """Return whether a corpus has already been installed."""

        with self._lock:
            return bool(self._chunks)

    def index_chunks(self, chunks: Sequence[TextChunk]) -> None:
        """Replace the complete in-memory corpus with the supplied chunks."""
        with self._lock:
            self._chunks = list(chunks)
            self._rebuild()

    def upsert_chunks(self, chunks: Sequence[TextChunk]) -> None:
        """Merge chunks by stable ID, retaining all previously indexed documents."""

        with self._lock:
            merged = {chunk.chunk_id: chunk for chunk in self._chunks}
            for chunk in chunks:
                merged[chunk.chunk_id] = chunk
            self._chunks = list(merged.values())
            self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild BM25 from ``_chunks`` while the caller holds ``_lock``."""

        self._tokenized = [tokenize(chunk.text) for chunk in self._chunks]
        self._fallback_bm25 = _SimpleBM25(self._tokenized)

        # rank-bm25 0.2.2 divides by zero while constructing BM25Okapi([]).
        # A fresh Qdrant collection legitimately hydrates to an empty corpus
        # immediately before the first upload is merged, so retain the empty
        # fallback until at least one chunk exists.
        if not self._tokenized:
            self._bm25 = self._fallback_bm25
            return

        try:
            from rank_bm25 import BM25Okapi  # noqa: PLC0415
        except ImportError:
            self._bm25 = self._fallback_bm25
        else:
            self._bm25 = BM25Okapi(self._tokenized)

    @forensic_span("coremesh.tool.bm25.search", SpanCategory.TOOL)
    def search(self, query: str, limit: int) -> list[SearchHit]:
        """Return positive-score lexical hits with deterministic tie ordering."""
        with self._lock:
            if not self._chunks or self._bm25 is None:
                return []

            query_tokens = tokenize(query)
            scores = self._bm25.get_scores(query_tokens)
            # rank-bm25 uses an Okapi IDF that can be negative for a tiny
            # corpus (notably one invoice page). That would erase a real
            # lexical match under the positive-score filter, so fall back to
            # the smoothed positive-IDF implementation for this query only.
            if not any(float(score) > 0 for score in scores):
                scores = self._fallback_bm25.get_scores(query_tokens)
            ranked = sorted(
                enumerate(scores),
                key=lambda item: (float(item[1]), -item[0]),
                reverse=True,
            )
            return [
                SearchHit(chunk=self._chunks[index], score=float(score))
                for index, score in ranked[:limit]
                if float(score) > 0
            ]


class CrossEncoderReranker:
    """Lazy sentence-transformers cross-encoder scoring adapter."""
    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.reranker_model
        self._model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415

            self._model = CrossEncoder(self.model_name)
        return self._model

    @forensic_span("coremesh.model.cross_encoder.rerank", SpanCategory.MODEL)
    def score(self, query: str, chunks: Sequence[TextChunk]) -> list[float]:
        if not chunks:
            return []
        scores = self.model.predict([(query, chunk.text) for chunk in chunks])
        return [float(score) for score in scores]


class LexicalReranker:
    """Deterministic token-frequency cosine scoring for hermetic validation."""

    def score(self, query: str, chunks: Sequence[TextChunk]) -> list[float]:
        query_counts = Counter(tokenize(query))
        query_norm = math.sqrt(sum(value * value for value in query_counts.values()))
        if query_norm == 0.0:
            return [0.0 for _chunk in chunks]

        scores: list[float] = []
        for chunk in chunks:
            chunk_counts = Counter(tokenize(chunk.text))
            chunk_norm = math.sqrt(sum(value * value for value in chunk_counts.values()))
            if chunk_norm == 0.0:
                scores.append(0.0)
                continue
            dot_product = sum(
                count * chunk_counts.get(token, 0)
                for token, count in query_counts.items()
            )
            scores.append(float(dot_product) / (query_norm * chunk_norm))
        return scores


def configured_embedding_provider() -> EmbeddingProvider:
    """Construct the configured production or hermetic embedding adapter."""

    if settings.rag_embedding_provider == "hash":
        return HashEmbeddingProvider()
    return OpenAIEmbeddingProvider()


def configured_reranker() -> Reranker:
    """Construct the configured production or hermetic reranking adapter."""

    if settings.rag_reranker_provider == "lexical":
        return LexicalReranker()
    return CrossEncoderReranker()


class HybridRetriever:
    """Facade for dense/sparse indexing, RRF fusion, and final reranking."""
    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        dense_index: DenseIndex | None = None,
        sparse_index: BM25SparseIndex | None = None,
        reranker: Reranker | None = None,
        *,
        dense_limit: int = 40,
        sparse_limit: int = 40,
        rrf_k: int = 60,
        rerank_limit: int = 20,
        dense_weight: float | None = None,
        sparse_weight: float | None = None,
        keyword_priority: bool | None = None,
    ) -> None:
        self.embedding_provider = embedding_provider or configured_embedding_provider()
        self.dense_index = dense_index or QdrantDenseIndex()
        self.sparse_index = sparse_index or BM25SparseIndex()
        self.reranker = reranker or configured_reranker()
        self.dense_limit = dense_limit
        self.sparse_limit = sparse_limit
        self.rrf_k = rrf_k
        self.rerank_limit = rerank_limit
        self.dense_weight = (
            dense_weight if dense_weight is not None else settings.rag_dense_weight
        )
        self.sparse_weight = (
            sparse_weight if sparse_weight is not None else settings.rag_sparse_weight
        )
        self.keyword_priority = (
            keyword_priority
            if keyword_priority is not None
            else settings.rag_keyword_priority
        )
        self._sparse_hydrated = self.sparse_index.has_chunks
        self._sparse_hydration_lock = threading.Lock()

    @forensic_span("coremesh.tool.rag.index", SpanCategory.TOOL)
    def index_chunks(self, chunks: Sequence[TextChunk]) -> None:
        """Embed once and upsert persistent dense and process-local sparse data."""
        chunk_list = list(chunks)
        if not chunk_list:
            return
        # Rebuild the sparse corpus before merging a new upload. Without this,
        # the first post-restart ingestion would mark hydration complete while
        # omitting older Qdrant documents from BM25.
        self._ensure_sparse_hydrated()
        embeddings = self.embedding_provider.embed([chunk.text for chunk in chunk_list])
        self.dense_index.index_chunks(chunk_list, embeddings)
        self.sparse_index.upsert_chunks(chunk_list)
        self._sparse_hydrated = True

    @forensic_span("coremesh.tool.rag.search", SpanCategory.TOOL)
    def search(self, query: str, top_k: int = 5) -> list[RetrievalResult]:
        """Return top evidence after weighted RRF and cross-encoder reranking."""
        if top_k <= 0:
            return []

        self._ensure_sparse_hydrated()
        query_vector = self.embedding_provider.embed([query])[0]
        dense_hits = self.dense_index.search(query_vector, self.dense_limit)
        sparse_hits = self.sparse_index.search(query, self.sparse_limit)
        fused = self._rrf_candidates(dense_hits, sparse_hits)

        candidates = sorted(
            fused.values(),
            key=lambda candidate: (
                candidate.rrf_score,
                candidate.sparse_rank is not None,
                -(candidate.sparse_rank or 10**9),
            ),
            reverse=True,
        )[: self.rerank_limit]
        if not candidates:
            return []

        rerank_scores = self.reranker.score(query, [candidate.chunk for candidate in candidates])
        if len(rerank_scores) != len(candidates):
            raise RuntimeError("Reranker returned a score count that does not match candidates.")
        query_technical = technical_tokens(query) if self.keyword_priority else set()

        def sort_key(item: tuple["_FusedCandidate", float]) -> tuple:
            candidate, rerank_score = item
            base = (
                float(rerank_score),
                candidate.rrf_score,
                candidate.sparse_rank is not None,
                -(candidate.sparse_rank or 10**9),
            )
            if self.keyword_priority:
                return (_technical_match_count(query_technical, candidate.chunk.text), *base)
            return base

        ranked = sorted(zip(candidates, rerank_scores), key=sort_key, reverse=True)

        return [
            RetrievalResult(
                chunk_id=candidate.chunk.chunk_id,
                text=candidate.chunk.text,
                source=candidate.chunk.source,
                metadata=candidate.chunk.metadata,
                reference_marker=f"[{candidate.chunk.source}:{candidate.chunk.chunk_id}]",
                score=float(rerank_score),
                dense_rank=candidate.dense_rank,
                sparse_rank=candidate.sparse_rank,
                rrf_score=candidate.rrf_score,
                rerank_score=float(rerank_score),
            )
            for candidate, rerank_score in ranked[:top_k]
        ]

    def _ensure_sparse_hydrated(self) -> None:
        """Rebuild BM25 from Qdrant once after a runtime restart."""

        if self._sparse_hydrated or self.sparse_index.has_chunks:
            self._sparse_hydrated = True
            return
        with self._sparse_hydration_lock:
            if self._sparse_hydrated or self.sparse_index.has_chunks:
                self._sparse_hydrated = True
                return
            loader = getattr(self.dense_index, "load_chunks", None)
            if callable(loader):
                self.sparse_index.index_chunks(loader())
            self._sparse_hydrated = True

    def _rrf_candidates(
        self,
        dense_hits: Sequence[SearchHit],
        sparse_hits: Sequence[SearchHit],
    ) -> dict[str, "_FusedCandidate"]:
        fused: dict[str, _FusedCandidate] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            candidate = fused.setdefault(hit.chunk.chunk_id, _FusedCandidate(chunk=hit.chunk))
            candidate.dense_rank = rank
            candidate.rrf_score += self.dense_weight / (self.rrf_k + rank)

        for rank, hit in enumerate(sparse_hits, start=1):
            candidate = fused.setdefault(hit.chunk.chunk_id, _FusedCandidate(chunk=hit.chunk))
            candidate.sparse_rank = rank
            candidate.rrf_score += self.sparse_weight / (self.rrf_k + rank)

        return fused


@dataclass
class _FusedCandidate:
    chunk: TextChunk
    rrf_score: float = 0.0
    dense_rank: int | None = None
    sparse_rank: int | None = None


class _SimpleBM25:
    """Small BM25Okapi-compatible fallback used before dependencies are installed."""

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.corpus = [list(doc) for doc in corpus]
        self.k1 = k1
        self.b = b
        self.doc_count = len(self.corpus)
        self.doc_lengths = [len(doc) for doc in self.corpus]
        self.avgdl = sum(self.doc_lengths) / self.doc_count if self.doc_count else 0.0
        self.term_freqs = [Counter(doc) for doc in self.corpus]
        document_freqs: Counter[str] = Counter()
        for doc in self.corpus:
            document_freqs.update(set(doc))
        self.idf = {
            term: math.log(1 + (self.doc_count - freq + 0.5) / (freq + 0.5))
            for term, freq in document_freqs.items()
        }

    def get_scores(self, query_tokens: Iterable[str]) -> list[float]:
        scores: list[float] = []
        query = list(query_tokens)
        for term_freq, doc_len in zip(self.term_freqs, self.doc_lengths):
            score = 0.0
            for token in query:
                frequency = term_freq.get(token, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (1 - self.b + self.b * doc_len / (self.avgdl or 1))
                score += self.idf.get(token, 0.0) * frequency * (self.k1 + 1) / denominator
            scores.append(score)
        return scores


def _hit_from_qdrant_point(point: Any) -> SearchHit:
    return SearchHit(
        chunk=_chunk_from_qdrant_payload(point.payload or {}),
        score=float(point.score),
    )


def _chunk_from_qdrant_payload(payload: dict[str, Any]) -> TextChunk:
    """Validate one Qdrant payload against the stable chunk contract."""

    return TextChunk(
        chunk_id=payload["chunk_id"],
        text=payload["text"],
        source=payload["source"],
        metadata=payload.get("metadata") or {},
    )


def _technical_match_count(query_tokens: set[str], text: str) -> int:
    if not query_tokens:
        return 0
    text_lower = text.lower()
    return sum(1 for token in query_tokens if token in text_lower)
