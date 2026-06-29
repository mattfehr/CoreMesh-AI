import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.rag.retrieval import (  # noqa: E402
    BM25SparseIndex,
    HybridRetriever,
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
