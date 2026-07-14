# Hybrid retrieval

This package is a library-only evidence retriever used directly by Python
callers and the RAG agent specialist. It does not ingest arbitrary documents,
generate a final answer, or expose HTTP endpoints.

## Indexing

<code>HybridRetriever.index_chunks</code> embeds every chunk with the configured
provider, upserts deterministic UUIDv5 points into a Qdrant cosine collection,
and replaces the process-local BM25 corpus. Chunk IDs should be stable across
re-indexing so dense points update rather than duplicate.

Qdrant persists. BM25 does not; a restarted process must rebuild the sparse
corpus before hybrid search.

## Search and ordering

Search embeds the query, fetches bounded dense and sparse candidate lists,
combines ranks with configurable weighted reciprocal-rank fusion, takes a
bounded rerank set, and applies the cross-encoder. Optional technical-token
priority puts exact compound identifiers ahead of pure model score, with RRF
and sparse rank as deterministic tie breakers.

Results include dense/sparse ranks, RRF and reranker scores, original metadata,
and a source/chunk reference marker. The module retrieves evidence only; a
caller must render and validate citations.

Default dependencies can call OpenAI and Qdrant and can download the
sentence-transformers model. Interfaces for embedding, dense index, and
reranking support isolated fakes in tests. A small internal BM25 implementation
keeps lexical behavior available when <code>rank_bm25</code> is absent.
