# Semantic response cache

This optional middleware reuses successful LLM responses for semantically
similar prompts without allowing materially different request settings to
share an entry.

## Eligibility and scope

Only JSON POST bodies with extractable prompt/message text are eligible.
Unified <code>/v1/execute</code> calls are always bypassed because they create
trace/memory side effects and may depend on changing corpus/database state.
Autopilot can explicitly bypass the cache for complex requests. Scope hashes
method, escaped path, routed model, system/developer content, all other request
parameters, and the streaming flag. Prompt similarity is evaluated only within
that exact scope.

The selected provider generates the query embedding. <code>openai</code> uses
the configured OpenAI-compatible embeddings endpoint and requires an API key
when caching is enabled. <code>hash</code> generates a deterministic,
L2-normalized signed feature hash locally; it exists for credential-free
development and validation, not production-quality semantic matching. Redis
Stack stores response hashes with TTL and searches their vectors through a
cosine-distance HNSW RediSearch index. Index initialization is mutex-protected
and retried after failure. The Redis namespace and vector width are configured
with <code>SEMANTIC_CACHE_KEY_PREFIX</code> and
<code>SEMANTIC_CACHE_VECTOR_DIM</code>; the selected embedder must produce that
exact width.

## Response behavior

A hit restores status, content type, body, and a cache header, then increments
hit count best-effort. A miss streams writes to the caller while capturing the
body and stores only 2xx results. Errors from embeddings, index setup, or lookup
produce a bypass; storage/counter errors do not change the response.

Each path writes <code>X-CoreMesh-Cache</code> as hit, miss, or bypass when
this middleware is enabled; the outer gateway uses that header for content-free
process counters. The cache favors availability, but cached bodies are still model output and may
contain sensitive content. TTL is not an authorization boundary. Preserve
scope hashing when adding fields, and never remove a request parameter from the
scope without proving that it cannot affect output.

Tests inject fake embedders/stores for policy behavior, exercise local hash
cache hits, and cover miniredis-compatible storage paths where applicable. No
live OpenAI call is required by unit tests.
