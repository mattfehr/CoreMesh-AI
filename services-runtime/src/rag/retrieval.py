"""Hybrid dense/sparse retrieval for CoreMesh Step 1.2.

The module keeps the production path wired to Qdrant, OpenAI embeddings, and a
cross-encoder reranker while allowing tests to inject lightweight fakes.
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

from pydantic import BaseModel, Field

from src.config import settings

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:[.\-/:][A-Za-z0-9_]+)*")


class TextChunk(BaseModel):
    chunk_id: str
    text: str
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
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
    chunk: TextChunk
    score: float


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class DenseIndex(Protocol):
    def index_chunks(self, chunks: Sequence[TextChunk], vectors: Sequence[Sequence[float]]) -> None:
        ...

    def search(self, query_vector: Sequence[float], limit: int) -> list[SearchHit]:
        ...


class Reranker(Protocol):
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
    tokens: set[str] = set()
    for token in TOKEN_PATTERN.findall(text):
        if any(sep in token for sep in "._-/:") or re.search(r"[a-z][A-Z]", token):
            tokens.add(token.lower())
    return tokens


def stable_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"coremesh:rag:{chunk_id}"))


class OpenAIEmbeddingProvider:
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

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(model=self.model, input=list(texts))
        return [item.embedding for item in response.data]


class QdrantDenseIndex:
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

    def ensure_collection(self) -> None:
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

    def index_chunks(self, chunks: Sequence[TextChunk], vectors: Sequence[Sequence[float]]) -> None:
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

    def search(self, query_vector: Sequence[float], limit: int) -> list[SearchHit]:
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=list(query_vector),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [_hit_from_qdrant_point(point) for point in response.points]


class BM25SparseIndex:
    def __init__(self) -> None:
        self._chunks: list[TextChunk] = []
        self._tokenized: list[list[str]] = []
        self._bm25: Any | None = None

    def index_chunks(self, chunks: Sequence[TextChunk]) -> None:
        self._chunks = list(chunks)
        self._tokenized = [tokenize(chunk.text) for chunk in self._chunks]

        try:
            from rank_bm25 import BM25Okapi  # noqa: PLC0415
        except ImportError:
            self._bm25 = _SimpleBM25(self._tokenized)
        else:
            self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, limit: int) -> list[SearchHit]:
        if not self._chunks or self._bm25 is None:
            return []

        query_tokens = tokenize(query)
        scores = self._bm25.get_scores(query_tokens)
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
    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.reranker_model
        self._model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415

            self._model = CrossEncoder(self.model_name)
        return self._model

    def score(self, query: str, chunks: Sequence[TextChunk]) -> list[float]:
        if not chunks:
            return []
        scores = self.model.predict([(query, chunk.text) for chunk in chunks])
        return [float(score) for score in scores]


class HybridRetriever:
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
        self.embedding_provider = embedding_provider or OpenAIEmbeddingProvider()
        self.dense_index = dense_index or QdrantDenseIndex()
        self.sparse_index = sparse_index or BM25SparseIndex()
        self.reranker = reranker or CrossEncoderReranker()
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

    def index_chunks(self, chunks: Sequence[TextChunk]) -> None:
        chunk_list = list(chunks)
        embeddings = self.embedding_provider.embed([chunk.text for chunk in chunk_list])
        self.dense_index.index_chunks(chunk_list, embeddings)
        self.sparse_index.index_chunks(chunk_list)

    def search(self, query: str, top_k: int = 5) -> list[RetrievalResult]:
        if top_k <= 0:
            return []

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
    payload = point.payload or {}
    chunk = TextChunk(
        chunk_id=payload["chunk_id"],
        text=payload["text"],
        source=payload["source"],
        metadata=payload.get("metadata") or {},
    )
    return SearchHit(chunk=chunk, score=float(point.score))


def _technical_match_count(query_tokens: set[str], text: str) -> int:
    if not query_tokens:
        return 0
    text_lower = text.lower()
    return sum(1 for token in query_tokens if token in text_lower)
