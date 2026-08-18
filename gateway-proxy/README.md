# CoreMesh gateway proxy

The gateway is the Go edge process in front of the Python runtime or another
configured provider-compatible upstream. It owns browser CORS, process-local
operational counters, request admission, model routing, optional semantic
caching, and primary/fallback resilience. It does not interpret documents or
execute runtime tools.

## Request order

~~~text
/healthz ------------------------------> local liveness response
/v1/observability ---------------------> local operational snapshot
CORS preflight ------------------------> local allowlist decision

all other paths
  -> response metadata counters
  -> autopilot routing (enabled by default)
  -> semantic cache (optional)
  -> Redis token bucket (mandatory)
  -> circuit-breaker route selection
  -> primary or fallback reverse proxy
~~~

The application wrapper handles preflight and observability before proxy
middleware. Proxied traffic is counted outside autopilot/cache, so final
response metadata is visible. Autopilot remains outside semantic cache:
complex requests can attach a bypass before lookup and rewritten models
participate in cache scope.

## Directory map

| Path | Responsibility |
| --- | --- |
| [cmd](cmd/README.md) | Process assembly, <code>/healthz</code>, listener, and fatal startup handling. |
| [internal/gateway](internal/gateway/README.md) | Redis token bucket, reverse proxies, and circuit breaker. |
| [internal/autopilot](internal/autopilot/README.md) | Complexity classification, model rewrite, and stable experiment assignment. |
| [internal/cache](internal/cache/README.md) | OpenAI or local hash embeddings and Redis Stack semantic response cache. |
| [internal/flags](internal/flags/README.md) | Placeholder for a general feature-flag engine; no code today. |
| [internal/registry](internal/registry/README.md) | Placeholder for prompt management; no code today. |
| [scripts](scripts/README.md) | Standard-library live routing and load verification tools. |
| <code>Dockerfile</code> | Multi-stage production binary image. |
| <code>go.mod</code> / <code>go.sum</code> | Go dependency manifest and generated integrity lock. |

## Startup and configuration

<code>cmd/main.go</code> reads configuration once, builds the handler, and
listens on <code>:8080</code>. Handler construction pings Redis and, when
<code>POSTGRES_DSN</code> is configured, PostgreSQL. Both checks are bounded
and fail startup when the selected dependency is unavailable. Restart the
process after environment changes. The local [.env.example](.env.example) is
for host development; Compose configuration lives at repository root.

### Admission and resilience

| Variable | Default | Meaning |
| --- | --- | --- |
| <code>GATEWAY_PRIMARY_URL</code> | <code>http://localhost:8000</code> | Normal upstream. |
| <code>GATEWAY_FALLBACK_URL</code> | <code>http://localhost:8000</code> | Open-circuit/error fallback; configure a different URL for real diversity. |
| <code>REDIS_URL</code> | <code>redis://localhost:6379</code> | Mandatory token-bucket Redis and optional cache store. |
| <code>RATE_LIMIT_CAPACITY</code> | <code>100</code> | Maximum tokens per identity bucket. |
| <code>RATE_LIMIT_REFILL_PER_SECOND</code> | <code>20</code> | Continuous token refill rate. |
| <code>CIRCUIT_FAILURE_THRESHOLD</code> | <code>5</code> | Primary failures needed within the window to open. |
| <code>CIRCUIT_FAILURE_WINDOW</code> | <code>30s</code> | Rolling primary-failure window. |
| <code>CIRCUIT_OPEN_DURATION</code> | <code>30s</code> | Time before one half-open primary probe. |
| <code>GATEWAY_ALLOWED_ORIGINS</code> | <code>http://localhost:3000,http://localhost:5173</code> | Exact comma-separated browser origins allowed by CORS. |

Durations use Go duration syntax. Invalid or non-positive values fail startup.
Origins must be complete HTTP(S) origins without paths, queries, or fragments.

### Autopilot

| Variable | Default | Meaning |
| --- | --- | --- |
| <code>AUTOPILOT_ENABLED</code> | <code>true</code> | Enable compatible JSON request classification/rewrite. |
| <code>AUTOPILOT_TIER1_MODEL</code> | <code>gpt-4o-mini</code> | Simple-request model. |
| <code>AUTOPILOT_TIER3_MODEL</code> | <code>gpt-4o</code> | Complex/baseline model. |
| <code>AUTOPILOT_EXPERIMENT_FLAG</code> | <code>cost_autopilot_routing</code> | PostgreSQL flag name. |
| <code>AUTOPILOT_EXPERIMENT_LOOKUP_TIMEOUT</code> | <code>2s</code> | Per-request experiment lookup deadline. |
| <code>POSTGRES_DSN</code> | empty | Enables reads from <code>feature_experiments</code> when set. |
| <code>AUTOPILOT_DEBUG</code> | <code>false</code> | Expose sanitized experiment lookup errors in a response header. |

### Semantic cache

| Variable | Default | Meaning |
| --- | --- | --- |
| <code>SEMANTIC_CACHE_ENABLED</code> | true only when <code>OPENAI_API_KEY</code> is nonempty | Explicit cache switch. |
| <code>SEMANTIC_CACHE_EMBEDDING_PROVIDER</code> | <code>openai</code> | <code>openai</code> for model embeddings or <code>hash</code> for deterministic local validation. |
| <code>OPENAI_API_KEY</code> | empty | Required only when the enabled cache uses the <code>openai</code> provider. |
| <code>OPENAI_BASE_URL</code> | <code>https://api.openai.com/v1</code> | Embedding API base. |
| <code>OPENAI_EMBEDDING_MODEL</code> | <code>text-embedding-3-small</code> | Must produce the configured 1536 dimensions. |
| <code>SEMANTIC_CACHE_INDEX</code> | <code>coremesh_semantic_cache</code> | RediSearch index name. |
| <code>SEMANTIC_CACHE_KEY_PREFIX</code> | <code>coremesh:semantic:entry:</code> | Redis namespace for response entries. |
| <code>SEMANTIC_CACHE_VECTOR_DIM</code> | <code>1536</code> | Vector width shared by the embedder and RediSearch index. |
| <code>SEMANTIC_CACHE_THRESHOLD</code> | <code>0.96</code> | Minimum cosine similarity in the interval (0, 1]. |
| <code>SEMANTIC_CACHE_TTL</code> | <code>24h</code> | Cached-response lifetime. |

<code>POST /v1/execute</code> always bypasses this cache because execution
creates traces/memory and can read freshness-sensitive SQL/RAG state.

## Request identity and response headers

Rate-limit identity preference is <code>X-Team-ID</code>,
<code>X-API-Key</code>, remote host, then an anonymous identity. Experiment
identity preference is <code>X-User-ID</code>, <code>X-Session-ID</code>,
<code>X-Team-ID</code>, <code>X-API-Key</code>, then remote host.

Important response headers include:

- <code>X-RateLimit-Remaining</code> and <code>Retry-After</code>;
- <code>X-CoreMesh-Route</code> and <code>X-CoreMesh-Circuit-State</code>;
- <code>X-CoreMesh-Autopilot-Tier</code>,
  <code>X-CoreMesh-Routed-Model</code>,
  <code>X-CoreMesh-Experiment-Variant</code>, and routing reason;
- <code>X-CoreMesh-Cache</code> with <code>hit</code>,
  <code>miss</code>, or <code>bypass</code>.

Allowlisted browser responses expose these headers. CORS preflights are handled
before admission and never consume rate-limit tokens or increment traffic
counters. A disallowed preflight receives 403.

## Local observability API

<code>GET /v1/observability</code> is served by the gateway itself and never
reaches the runtime. It reports gateway start/snapshot timestamps, admission
configuration, cache hit/miss/bypass counters, current circuit
state/configuration, and total/primary/fallback/rate-limited/error traffic.
Cache hit rate is <code>hits / (hits + misses)</code> and is
<code>null</code> before eligible traffic.

Counters are thread-safe, content-free, process-local, and reset on restart.
The observability request itself and CORS preflights are excluded. Mutation
methods receive 405 and snapshots use <code>Cache-Control: no-store</code>.
See each internal package guide for exact contracts.

## Failure policy and side effects

- Redis admission errors fail closed with 503; an exhausted bucket returns 429.
- Autopilot ignores ineligible payloads. A configured experiment lookup error
  uses tier-3 baseline routing.
- Cache embed/index/search errors fail open to upstream. Cache writes are
  best-effort and never replace a successful upstream response.
- Primary transport failures and 5xx responses feed the rolling circuit.
  Before the threshold they can return an error; the threshold-crossing request
  and open-circuit traffic use fallback.
- Middleware buffers eligible JSON and replayable proxy bodies in memory.
- Redis stores rate-limit/cached-response state; PostgreSQL may be read per
  routed request; OpenAI embeddings transmit prompt text and incur cost. The
  hash provider makes no external request, but is intended for development and
  hermetic validation rather than production-quality semantic similarity.

There is no authentication, TLS termination, or request-size limit in this
binary. Put those controls at a trusted outer edge for production use.

## Run and verify

From this directory, with Redis and the upstream running:

~~~powershell
go run ./cmd
go test ./...
python scripts/verify_autopilot_routing.py --url http://localhost:8080/v1/chat/completions
python scripts/load_test_200.py --url http://localhost:8080/health
~~~

The scripts have additional prerequisites and expected outcomes in
[scripts/README.md](scripts/README.md).
