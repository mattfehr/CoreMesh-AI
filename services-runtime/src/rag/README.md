# Hybrid retrieval

This package is an evidence-retrieval library used by trusted Python callers
and the RAG agent specialist. The runtime's restricted
<code>/v1/execute</code> RAG mode invokes that specialist, while opt-in
<code>/v1/ingest</code> indexing feeds page chunks to the same
application-scoped retriever. This package mounts no endpoint and does not
generate a final answer.

## Indexing

<code>HybridRetriever.index_chunks</code> embeds every chunk with the configured
provider, upserts deterministic UUIDv5 points into a Qdrant cosine collection,
and merges chunks by stable ID into the process-local BM25 corpus. Existing
documents remain searchable; re-indexing a stable chunk updates it rather than
duplicating it or dropping other documents.

Qdrant persists text and metadata payloads; BM25 remains process-local. On the
first search or index operation after restart, the retriever lazily scrolls the
Qdrant payloads and rebuilds BM25 before merging new chunks. Callers do not
perform a separate sparse-index rebuild.

## Search and ordering

Search embeds the query, fetches bounded dense and sparse candidate lists,
combines ranks with configurable weighted reciprocal-rank fusion, takes a
bounded rerank set, and applies the configured reranker. Optional technical-token
priority puts exact compound identifiers ahead of pure model score, with RRF
and sparse rank as deterministic tie breakers.

Results include dense/sparse ranks, RRF and reranker scores, original metadata,
and a source/chunk reference marker. The module retrieves evidence only; a
caller must render and validate citations.

<code>RAG_EMBEDDING_PROVIDER=openai|hash</code> selects OpenAI embeddings or
deterministic normalized feature hashing. The hash provider gives lexical
similarity for hermetic validation, not production semantic quality.
<code>RAG_RERANKER_PROVIDER=cross_encoder|lexical</code> selects the configured
sentence-transformers model or deterministic token-frequency cosine scoring.
Hash plus lexical mode needs no provider credential or model download, though
Qdrant remains the dense store. OpenAI plus cross-encoder remains the default.

Interfaces for embedding, dense indexing, and reranking support isolated fakes
in tests. A small internal BM25 implementation keeps lexical behavior available
when <code>rank_bm25</code> is absent.
