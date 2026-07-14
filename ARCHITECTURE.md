# CoreMesh architecture

This document describes the architecture implemented in this repository: the
live process boundaries, request flows, state owners, external calls, and
failure policies. The project blueprint remains design context for future
phases; it does not override the checked-in code.

## System boundaries

CoreMesh currently has three runtime layers:

| Layer | Owned responsibility | Explicitly outside the layer |
| --- | --- | --- |
| Go gateway | Edge admission, model routing, optional semantic caching, rate limiting, and primary/fallback resilience. | Document interpretation, retrieval generation, SQL execution, and agent work. |
| Python runtime | HTTP ingestion plus reusable retrieval, SQL, orchestration, memory, and arbitration libraries. | Edge traffic policy and persistent infrastructure lifecycle. |
| Local data stack | PostgreSQL metadata, Redis operational state, and Qdrant vectors. | Starting the gateway/runtime or scheduling background work. |

The analytics directories and GitHub workflows are placeholders. They have no
runtime edge into these layers today.

## Gateway request flow

The executable gateway is <code>gateway-proxy/cmd/main.go</code>. Startup loads
configuration, verifies Redis connectivity, constructs optional middleware,
and listens on port 8080. Startup fails closed if configuration or mandatory
Redis access is invalid.

<code>GET /healthz</code> is handled by the top-level multiplexer and bypasses
all middleware. Every other path follows this order:

~~~text
HTTP request
    |
    v
Autopilot router
    |  compatible JSON POST:
    |  classify complexity, select/rewrite model,
    |  attach routing/cache-policy headers,
    |  optionally resolve stable PostgreSQL experiment split
    v
Semantic cache (only when enabled)
    |  bypass ineligible/complex/error paths
    |  return an eligible Redis vector hit
    |  or forward and capture a successful miss
    v
Redis token bucket
    |  reject exhausted identity with 429
    |  reject Redis errors with 503
    v
Circuit breaker and reverse proxy
    |  closed: primary
    |  open: fallback
    |  half-open: one primary probe; concurrent traffic uses fallback
    v
Configured upstream
~~~

### Autopilot invariants

Autopilot only rewrites JSON POST bodies from which it can extract prompt or
message text. Other requests pass through unchanged. Complexity score 3 or
higher selects tier 3 and asks the semantic cache to bypass; simpler requests
select tier 1. The model field is rewritten before the cache computes scope.

When a PostgreSQL experiment store is configured, rollout assignment hashes a
stable identity into 100 buckets. Experiment lookup failure degrades to the
tier-3 baseline rather than risking an experimental route. Debug errors are
placed in headers only when explicitly enabled.

### Semantic-cache invariants

The cache key scope includes method, escaped path, routed model, system/developer
message hash, non-message parameter hash, and streaming flag. Thus a similar
prompt cannot cross material request settings. Redis Stack performs HNSW
similarity lookup over OpenAI embeddings.

Embedding, index, and lookup failures fail open to the upstream and report a
cache bypass. Only successful 2xx responses are stored, and store/hit-counter
write failures do not replace the upstream response. This favors availability
over cache completeness. Cached model output is persistent application data
for the configured TTL and must be treated accordingly.

### Admission and resilience invariants

Rate-limit identity preference is <code>X-Team-ID</code>,
<code>X-API-Key</code>, remote host, then an anonymous bucket. The Lua script
uses Redis server time so distributed gateway instances update one token bucket
atomically.

The circuit breaker counts primary transport errors and 5xx responses in a
rolling window. Opening routes traffic to fallback for the configured
duration. After that duration, exactly one half-open request probes primary;
other concurrent requests continue to fallback. Request bodies are buffered
and made replayable so the threshold-crossing request can be retried safely at
fallback. This buffering means request size must be controlled by the caller or
an outer proxy.

The default primary and fallback URLs are both the local Python runtime. A
deployment only gains provider diversity after it configures distinct URLs.

## Document-ingestion flow

FastAPI exposes the implemented runtime surface from
<code>services-runtime/src/main.py</code>. The ingestion route validates the
declared multipart content type, reads the upload into memory, and moves the
blocking processing pipeline to a worker thread so the event loop remains
responsive.

~~~text
POST /v1/ingest
    |
    |-- reject unsupported media type (415)
    |-- reject empty upload (400)
    v
Load pages
    |-- PDF: pdf2image + Poppler at 300 DPI
    |-- raster: Pillow decode to RGB
    v
Preprocess each page
    |-- grayscale / deskew / threshold / denoise
    v
OCR ensemble
    |-- Tesseract
    |-- EasyOCR
    |-- compare normalized edit-distance variance
    |-- optional OpenAI vision fallback above threshold
    v
Structured extraction
    |-- OpenAI + Instructor when a key is configured
    |-- deterministic invoice regex path otherwise
    v
Invoice-total validation
    |
    v
IngestResponse with provenance, flags, validation, pages, and timing
~~~

Vision fallback failure preserves the best OCR candidate. By contrast, an
enabled LLM structured-extraction failure propagates and the route returns 422;
it does not silently switch to regex. The offline regex parser is intentionally
specialized for the canonical invoice layout used by the verification script,
not a general replacement for model extraction.

Uploads, rendered PDF pages, and OCR arrays are held in process memory. OCR can
consume substantial CPU, and EasyOCR may initialize/download model weights on
first use. When OpenAI-backed paths are active, document content leaves the
local trust boundary and calls can incur cost.

## Library-only runtime flows

The following components are implemented and tested but are not mounted as
FastAPI routes. Their callers are responsible for authentication, authorization,
input limits, cancellation, and lifecycle management.

### Hybrid retrieval

<code>HybridRetriever.index_chunks</code> creates OpenAI embeddings, upserts
stable UUIDv5 points into Qdrant, and builds an in-process BM25 corpus.
<code>search</code> retrieves dense and sparse candidates, combines ranks with
weighted reciprocal-rank fusion, reranks a bounded candidate set with a
cross-encoder, optionally promotes exact technical identifiers, and emits
source markers.

The BM25 index is process-local and must be rebuilt after restart. Qdrant is
persistent. The embedding and reranker paths can perform network calls and
model downloads respectively.

### Guarded SQL

<code>SQLSandbox</code> introspects the configured SQLAlchemy database and
accepts exactly one parsed SELECT statement. It rejects blocked mutation/admin
keywords and dangerous functions, adds a row limit when absent, starts a
transaction, requests read-only mode, executes, materializes all allowed rows,
and always rolls the transaction back.

Lexical validation is defense in depth, not a substitute for a database role
that has read-only privileges. The default DSN uses the local development
database owner and should not be copied into a production security boundary.

### Supervisor orchestration and memory

The orchestrator builds a deterministic plan from request cues and dispatches
RAG search, document extraction, and SQL generation specialists through a
LangGraph state machine. If LangGraph is unavailable, a sequential graph with
the same node contracts is used.

Redis short-term memory stores session state and events under expiring keys.
Chroma long-term memory stores a summary after completion using deterministic
local hash embeddings. Specialist errors become observations so later steps and
the synthesized result can report partial failure. After synthesis, consensus
arbitration may replace or block the deliverable.

One specialist accepts document bytes, base64, text, or a filesystem path from
session context. Any future network endpoint exposing that feature must
validate path access; the library itself assumes a trusted caller.

### Consensus arbitration

The arbitrator evaluates outgoing text with factual, logic, and completeness
critics in parallel. Provider calls use bounded retries and an overall timeout.
At least two successful assessments form quorum. Low scores, anomalies, or a
critic failure trigger adjudication; insufficient quorum, timeout, or
adjudicator failure blocks delivery for review.

The default clients can call OpenAI, Anthropic, and Ollama. A clean critic pass
releases the original text, while adjudication can release, remediate, block,
or request manual review. No HTTP route or durable review queue currently
consumes manual-review verdicts.

## State and external side effects

| State or dependency | Writer or caller | Lifetime and notes |
| --- | --- | --- |
| Redis rate-limit hashes | Gateway token bucket | Expiring keys; Redis is mandatory at gateway startup. |
| Redis semantic hashes/vector index | Optional gateway cache | Successful model responses persist until TTL/volume deletion. |
| PostgreSQL feature experiments | External control plane; gateway reads | Tables are bootstrapped by <code>init.sql</code>; no management API exists. |
| PostgreSQL query target | SQL sandbox | Read-only transaction requested, results materialized, transaction rolled back. |
| Qdrant collection | RAG dense index | Persistent named volume in Compose. |
| In-process BM25 corpus | RAG sparse index | Lost at process exit. |
| Redis agent sessions/events | Orchestrator | TTL controlled by runtime settings. |
| Chroma agent summaries | Orchestrator | Local persistent directory; deterministic interaction ID upserts. |
| OpenAI APIs | Ingestion, RAG, arbitration, gateway cache | External transmission, latency, rate limits, and cost. |
| Anthropic API / Ollama | Arbitration | External or local provider calls when arbitration runs. |
| OCR/model caches | EasyOCR and sentence-transformers | May download and persist model weights outside the repository. |
| Docker named volumes/network | Docker Compose | Created on <code>up</code>; data survives container recreation. |

## Configuration lifecycle

The Python settings object reads environment variables and a service-local
<code>.env</code> file during module import. Changes require a process reload.
Most heavyweight clients are lazy and connect only when their feature is
invoked.

The gateway reads environment variables once during startup. Redis is connected
and pinged immediately. Semantic-cache configuration can require OpenAI
credentials; autopilot is on by default and opens a PostgreSQL pool only when a
DSN is supplied. Restart the gateway after configuration changes.

Detailed variable names, defaults, and validation rules live in the gateway
and runtime READMEs next to their configuration code.

## Implemented versus planned

The repository intentionally carries structural placeholders from the broader
blueprint:

- <code>gateway-proxy/internal/flags</code> and
  <code>gateway-proxy/internal/registry</code> contain no implementation.
- <code>services-runtime/src/tracing</code> contains no forensics code.
- <code>analytics-workers</code> contains no worker entry point or dependencies.
- the two GitHub workflow files have empty events and jobs.
- there is no frontend or human-review application.

When one of these becomes real, update its README, file headers, this document,
the root status table, and the nearest tests in the same change. Follow
[DOCUMENTATION.md](DOCUMENTATION.md) to keep the codebase self-documenting.
