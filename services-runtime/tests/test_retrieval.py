"""Hybrid retrieval ranking and index-contract tests.

System role:
    Protects tokenization, stable IDs, dense/sparse fusion, reranking, technical
    identifier priority, and deterministic result metadata.
Dependencies:
    pytest-compatible discovery and injected in-memory embedding/index fakes.
Side effects:
    None outside process memory; no OpenAI, Qdrant, or model download occurs.
"""

import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.rag.retrieval import (  # noqa: E402
    BM25SparseIndex,
    HashEmbeddingProvider,
    HybridRetriever,
    LexicalReranker,
    SearchHit,
    TextChunk,
    tokenize,
)


class FakeEmbeddingProvider:
    def embed(self, texts):
        return [[float(index + 1)] for index, _text in enumerate(texts)]


class FakeDenseIndex:
    def __init__(self):
        self._chunks = {}

    def index_chunks(self, chunks, vectors):
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}

    def search(self, query_vector, limit):
        return [
            SearchHit(chunk=self._chunks["semantic"], score=0.99),
            SearchHit(chunk=self._chunks["exact"], score=0.84),
            SearchHit(chunk=self._chunks["generic"], score=0.62),
        ][:limit]


class FakeReranker:
    def score(self, query, chunks):
        return [0.5 for _chunk in chunks]


def test_bm25_identifier_match_overrides_basic_semantic_lookup():
    chunks = [
        TextChunk(
            chunk_id="semantic",
            source="ops-guide",
            text=(
                "Circuit breaker fallback behavior protects the gateway when "
                "downstream LLM failures exceed the configured threshold."
            ),
        ),
        TextChunk(
            chunk_id="exact",
            source="ops-guide",
            text=(
                "When CircuitBreakerState.OPEN is set, the gateway blocks primary "
                "LLM calls and routes traffic to the configured fallback provider."
            ),
        ),
        TextChunk(
            chunk_id="generic",
            source="ops-guide",
            text="Token bucket counters reset after each rate-limit refill interval.",
        ),
    ]
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(),
        dense_index=FakeDenseIndex(),
        sparse_index=BM25SparseIndex(),
        reranker=FakeReranker(),
    )
    retriever.index_chunks(chunks)

    started = time.perf_counter()
    results = retriever.search("How does CircuitBreakerState.OPEN affect routing?", top_k=5)
    elapsed_ms = (time.perf_counter() - started) * 1_000

    assert elapsed_ms < 200
    assert results[0].chunk_id == "exact"
    assert results[0].sparse_rank == 1
    assert results[0].reference_marker == "[ops-guide:exact]"


def test_tokenize_preserves_term_frequency_and_splits_identifiers():
    tokens = tokenize("retry retry CircuitBreakerState.OPEN")

    # Term frequency must survive (BM25 relies on it): two "retry" occurrences.
    assert tokens.count("retry") == 2

    # Compound identifier is emitted whole and split into parts exactly once.
    assert tokens.count("circuitbreakerstate.open") == 1
    assert tokens.count("circuitbreakerstate") == 1
    assert tokens.count("open") == 1

    # A plain single-part token is not duplicated by the split branch.
    assert tokenize("open").count("open") == 1


def _make_retriever(**kwargs):
    chunks = [
        TextChunk(
            chunk_id="semantic",
            source="ops-guide",
            text=(
                "Circuit breaker fallback behavior protects the gateway when "
                "downstream LLM failures exceed the configured threshold."
            ),
        ),
        TextChunk(
            chunk_id="exact",
            source="ops-guide",
            text=(
                "When CircuitBreakerState.OPEN is set, the gateway blocks primary "
                "LLM calls and routes traffic to the configured fallback provider."
            ),
        ),
        TextChunk(
            chunk_id="generic",
            source="ops-guide",
            text="Token bucket counters reset after each rate-limit refill interval.",
        ),
    ]
    reranker = kwargs.pop("reranker", None) or FakeReranker()
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(),
        dense_index=FakeDenseIndex(),
        sparse_index=BM25SparseIndex(),
        reranker=reranker,
        **kwargs,
    )
    retriever.index_chunks(chunks)
    return retriever


class PreferSemanticReranker:
    """Reranker that strongly prefers the identifier-free 'semantic' chunk."""

    def score(self, query, chunks):
        weights = {"semantic": 0.9, "exact": 0.1, "generic": 0.0}
        return [weights.get(chunk.chunk_id, 0.0) for chunk in chunks]


def test_keyword_priority_toggle_controls_override():
    query = "How does CircuitBreakerState.OPEN affect routing?"

    # ON (default): the exact identifier match is promoted above the reranker's
    # preferred chunk.
    with_priority = _make_retriever(reranker=PreferSemanticReranker(), keyword_priority=True)
    assert with_priority.search(query, top_k=5)[0].chunk_id == "exact"

    # OFF: ranking falls back to the cross-encoder score, so the reranker's
    # favorite wins instead.
    without_priority = _make_retriever(reranker=PreferSemanticReranker(), keyword_priority=False)
    assert without_priority.search(query, top_k=5)[0].chunk_id == "semantic"


def test_rrf_weights_are_applied():
    dense_only = _make_retriever(dense_weight=1.0, sparse_weight=0.0, keyword_priority=False)
    results = dense_only.search("CircuitBreakerState.OPEN", top_k=5)

    # With sparse contribution zeroed, the BM25-only "exact" hit gets no rrf
    # mass from the sparse list, so its fused score reflects dense ranking only.
    exact = next(r for r in results if r.chunk_id == "exact")
    semantic = next(r for r in results if r.chunk_id == "semantic")
    assert semantic.rrf_score > exact.rrf_score


class PersistentFakeDenseIndex:
    """Qdrant-shaped fake whose payloads survive retriever construction."""

    def __init__(self, chunks=()):
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self.load_calls = 0

    def index_chunks(self, chunks, vectors):
        assert len(chunks) == len(vectors)
        self._chunks.update({chunk.chunk_id: chunk for chunk in chunks})

    def search(self, query_vector, limit):
        return [
            SearchHit(chunk=chunk, score=1.0 / rank)
            for rank, chunk in enumerate(self._chunks.values(), start=1)
        ][:limit]

    def load_chunks(self):
        self.load_calls += 1
        return list(self._chunks.values())


def test_hash_embeddings_are_deterministic_and_unit_normalized():
    provider = HashEmbeddingProvider(vector_size=32)

    first, second, blank = provider.embed(["Acme invoice 100", "Acme invoice 100", ""])

    assert first == second
    assert len(first) == 32
    assert math.isclose(sum(value * value for value in first), 1.0)
    assert math.isclose(sum(value * value for value in blank), 1.0)


def test_hash_embedding_rejects_nonpositive_dimensions():
    with pytest.raises(ValueError, match="positive"):
        HashEmbeddingProvider(vector_size=0)


def test_lexical_reranker_prefers_token_overlap():
    chunks = [
        TextChunk(chunk_id="match", source="invoice", text="Acme Software License"),
        TextChunk(chunk_id="other", source="invoice", text="Travel and lodging"),
    ]

    scores = LexicalReranker().score("Acme license", chunks)

    assert scores[0] > scores[1]
    assert scores[1] == 0.0


def test_bm25_rehydrates_from_persisted_dense_payloads_after_restart():
    persisted = TextChunk(
        chunk_id="invoice-page-1",
        source="invoice.png",
        text="Acme Corp Software License invoice total 108 dollars",
        metadata={"document_id": "doc-1", "page_number": 1},
    )
    dense = PersistentFakeDenseIndex([persisted])
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(),
        dense_index=dense,
        sparse_index=BM25SparseIndex(),
        reranker=LexicalReranker(),
    )

    first = retriever.search("Acme Software License", top_k=1)
    second = retriever.search("Acme Software License", top_k=1)

    assert dense.load_calls == 1
    assert first[0].chunk_id == persisted.chunk_id
    assert first[0].dense_rank == 1
    assert first[0].sparse_rank == 1
    assert second[0].sparse_rank == 1


def test_first_ingest_after_restart_merges_with_persisted_sparse_corpus():
    old_chunk = TextChunk(
        chunk_id="old",
        source="old.pdf",
        text="Historic renewal policy",
    )
    new_chunk = TextChunk(
        chunk_id="new",
        source="new.pdf",
        text="Current software invoice",
    )
    dense = PersistentFakeDenseIndex([old_chunk])
    sparse = BM25SparseIndex()
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(),
        dense_index=dense,
        sparse_index=sparse,
        reranker=LexicalReranker(),
    )

    retriever.index_chunks([new_chunk])

    assert dense.load_calls == 1
    assert sparse.search("historic renewal", limit=5)[0].chunk.chunk_id == "old"
    assert sparse.search("current software", limit=5)[0].chunk.chunk_id == "new"


def test_first_ingest_survives_empty_persisted_corpus_with_rank_bm25(monkeypatch):
    """Fresh Qdrant hydration must not construct BM25Okapi with no documents."""

    class GuardBM25:
        def __init__(self, corpus):
            assert corpus, "BM25Okapi must not receive an empty corpus"
            self._corpus = corpus

        def get_scores(self, query_tokens):
            query = set(query_tokens)
            return [float(len(query.intersection(document))) for document in self._corpus]

    monkeypatch.setitem(sys.modules, "rank_bm25", SimpleNamespace(BM25Okapi=GuardBM25))
    dense = PersistentFakeDenseIndex()
    sparse = BM25SparseIndex()
    retriever = HybridRetriever(
        embedding_provider=FakeEmbeddingProvider(),
        dense_index=dense,
        sparse_index=sparse,
        reranker=LexicalReranker(),
    )
    first_chunk = TextChunk(
        chunk_id="first:page:1",
        source="first.png",
        text="Acme Software License invoice",
    )

    retriever.index_chunks([first_chunk])

    assert dense.load_calls == 1
    assert sparse.search("Software License", limit=5)[0].chunk.chunk_id == first_chunk.chunk_id


def test_sparse_upsert_replaces_same_chunk_without_dropping_other_documents():
    sparse = BM25SparseIndex()
    sparse.upsert_chunks(
        [
            TextChunk(chunk_id="doc-a:1", source="a", text="original alpha"),
            TextChunk(chunk_id="doc-b:1", source="b", text="retained beta"),
        ]
    )
    sparse.upsert_chunks(
        [TextChunk(chunk_id="doc-a:1", source="a", text="updated gamma")]
    )

    assert sparse.search("original", limit=5) == []
    assert sparse.search("updated", limit=5)[0].chunk.chunk_id == "doc-a:1"
    assert sparse.search("retained", limit=5)[0].chunk.chunk_id == "doc-b:1"


def test_single_document_sparse_match_survives_negative_library_idf():
    class NegativeScoreBM25:
        def get_scores(self, _query_tokens):
            return [-0.25]

    sparse = BM25SparseIndex()
    sparse.index_chunks(
        [
            TextChunk(
                chunk_id="invoice:page:1",
                source="invoice.png",
                text="Acme Software License invoice",
            )
        ]
    )
    sparse._bm25 = NegativeScoreBM25()

    hits = sparse.search("Software License", limit=5)

    assert [hit.chunk.chunk_id for hit in hits] == ["invoice:page:1"]
    assert hits[0].score > 0
