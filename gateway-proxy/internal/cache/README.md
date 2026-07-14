# Semantic response cache

This optional middleware reuses successful LLM responses for semantically
similar prompts without allowing materially different request settings to
share an entry.

## Eligibility and scope

Only JSON POST bodies with extractable prompt/message text are eligible.
Autopilot can explicitly bypass the cache for complex requests. Scope hashes
method, escaped path, routed model, system/developer content, all other request
parameters, and the streaming flag. Prompt similarity is evaluated only within
that exact scope.

OpenAI generates the query embedding. Redis Stack stores response hashes with
TTL and searches their vectors through a cosine-distance HNSW RediSearch index.
Index initialization is mutex-protected and retried after failure.

## Response behavior

A hit restores status, content type, body, and a cache header, then increments
hit count best-effort. A miss streams writes to the caller while capturing the
body and stores only 2xx results. Errors from embeddings, index setup, or lookup
produce a bypass; storage/counter errors do not change the response.

The cache favors availability, but cached bodies are still model output and may
contain sensitive content. TTL is not an authorization boundary. Preserve
scope hashing when adding fields, and never remove a request parameter from the
scope without proving that it cannot affect output.

Tests inject fake embedders/stores for policy behavior and miniredis-compatible
storage paths where applicable. No live OpenAI call is required by unit tests.
