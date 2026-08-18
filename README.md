# CoreMesh single-host deployment

CoreMesh is a single-host AI engineering stack built from a Go gateway, Python
runtime, React operations UI, Redis Stack, PostgreSQL, Qdrant, and an optional
PostgreSQL-backed analytics worker. This is the deployment and end-to-end
validation guide for the local, demo, and CI Compose topology.

The stack is intentionally easy to validate without model credentials. It is
not production hardened: the Compose topology has no TLS, authentication, or
tenant isolation. Review [Production hardening](#production-hardening) before
exposing it beyond a trusted host.

The checked-in code and subsystem READMEs are the source of truth. See
[ARCHITECTURE.md](ARCHITECTURE.md) for detailed data flows and state ownership;
the original [project blueprint](plan/coremesh.txt) describes a larger target
system rather than current implementation status.

## What runs today

- The Go gateway on port 8080 provides Redis-backed token-bucket admission,
  primary/fallback reverse proxying, a concurrency-safe circuit breaker,
  request-complexity model routing, experiment splits, and an optional semantic
  response cache. It also owns the browser CORS allowlist and a content-free,
  process-local observability snapshot.
- The FastAPI runtime on port 8000 exposes liveness, document-ingestion, and a
  minimal OpenAI-shaped <code>/v1/chat/completions</code> path (deterministic
  stub unless live OpenAI is enabled), plus restricted unified execution and
  redacted forensic trace APIs. Ingestion performs image preprocessing,
  dual-engine OCR, optional vision fallback, structured extraction, and
  invoice-total validation.
- The React dashboard on port 3000 provides RAG/SQL/agent execution, gateway
  metrics, and interactive OpenTelemetry trace trees. One centralized browser
  client calls only the Go gateway on port 8080.
- Docker Compose starts PostgreSQL, Redis Stack, and Qdrant by default. An
  <code>app</code> profile boots runtime, gateway, and frontend with a named
  forensic trace volume; an <code>analytics</code> profile adds the scheduled
  production log miner.
- Failure forensics and the opt-in production log feedback loop are
  implemented. Model-regression CI, guarded self-healing documentation, and
  feature-scoped PEFT/QLoRA training are live. Prompt/flag packages remain
  documented placeholders.

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed flows and state ownership.

## Runtime topology

~~~text
Browser UI :3000
  |
  v
Go gateway :8080
  |-- /healthz ------------------------------------> local response
  |-- /v1/observability ----------------------------> local counters/config
  |-- CORS preflight -------------------------------> local allowlist decision
  |
  |-- autopilot model routing
  |-- optional semantic cache
  |-- Redis token-bucket admission
  |-- circuit-breaker primary/fallback selection
  |
  v
Python runtime :8000
  |-- /health
  |-- /v1/ingest
  |-- /v1/chat/completions
  |-- /v1/execute
  |-- /v1/traces
  |-- /v1/traces/{trace_id}
  |
  +--> RAG / guarded SQL / agent supervisor / OCR / optional model calls

Shared and optional state:
  Redis Stack <---- rate limits and semantic cache
  PostgreSQL  <---- experiments, redacted interaction logs, eval datasets
  Qdrant      <---- hybrid-retrieval vectors
  Chroma      <---- long-term agent memory when orchestration is invoked
  trace volume <--- redacted JSON artifacts and SQLite registry

Optional analytics profile:
  Runtime -- redacted failures --> PostgreSQL --> daily HDBSCAN log miner
                                                --> golden_datasets / review
~~~

The log miner reads redacted rows from `production_interaction_logs` over its
PostgreSQL connection. It does not consume runtime trace files, so the
`runtime-traces` forensic JSON/SQLite volume is deliberately mounted only in
the runtime. The worker needs the shared Compose network and PostgreSQL schema,
not a trace-volume mount.

Middleware order matters. CORS/observability terminate locally, while other
requests enter process metrics, autopilot, the optional semantic cache, then
rate limiting and proxy resilience. This lets complex requests declare a cache
bypass before lookup, ensures cache misses pass through admission, and records
the final cache/route/circuit headers.

## Repository map

| Path | Responsibility |
| --- | --- |
| [gateway-proxy](gateway-proxy/README.md) | Go edge admission, routing, caching, and upstream resilience. |
| [services-runtime](services-runtime/README.md) | FastAPI ingestion, unified execution/trace APIs, and intelligent-runtime libraries. |
| [frontend-ui](frontend-ui/README.md) | React execution, observability, and forensic trace dashboard. |
| [analytics-workers](analytics-workers/README.md) | Scheduled log mining, model-regression evaluation, and PEFT/QLoRA adapter training. |
| [.github](.github/README.md) | Repository automation for model-regression gating and guarded self-healing documentation. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Cross-service flows, state, failure boundaries, and implementation status. |
| [DOCUMENTATION.md](DOCUMENTATION.md) | Required file headers, docstrings, comments, directory READMEs, and upkeep checklist. |
| <code>docker-compose.yml</code> | Local data services plus opt-in <code>app</code> and <code>analytics</code> profiles. |
| <code>init.sql</code> | First-boot PostgreSQL schema for prompts, experiments, redacted logs, candidates, and golden datasets. |
| [integration_test.sh](integration_test.sh) | Isolated, credential-free seven-stage Compose acceptance test. |

Each major source and test directory has its own README. Start there before
changing a subsystem; it records local invariants, dependencies, side effects,
and focused test commands.

## Prerequisites

- Docker Engine or Docker Desktop with Docker Compose v2
- Enough memory and disk for application images and persistent volumes
- Network access while Docker builds or pulls images
- Bash, `curl`, and Python 3 only for the master integration script

Go 1.22, Python 3.11, Node.js 22, Tesseract, and Poppler are needed only for
the corresponding host-development workflows. The service images include
their runtime dependencies. EasyOCR and cross-encoder modes can download model
weights on first use.

## Configure Compose

Copy the root template and set a unique PostgreSQL password before invoking
Compose:

~~~powershell
Copy-Item .env.example .env
~~~

~~~bash
cp .env.example .env
~~~

`.env.example` intentionally leaves secrets blank. `POSTGRES_PASSWORD` is
required and Compose interpolation fails immediately when it is missing. Keep
`.env` out of version control. Use a URL-safe password because Compose also
interpolates it into PostgreSQL DSNs.

Validate the resolved configuration before creating containers:

~~~bash
docker compose config --quiet
~~~

All published ports bind to `COREMESH_BIND_ADDRESS=127.0.0.1` by default.
Override the `*_HOST_PORT` values when a port is occupied. If the gateway or
frontend port changes, keep `VITE_GATEWAY_BASE_URL` and
`GATEWAY_ALLOWED_ORIGINS` aligned and rebuild the frontend image.

The root `.env` is the Compose contract. Service-local example files are for
host development and intentionally use host-oriented addresses and blank
secrets; do not copy their connection strings into Compose.

### Provider modes

External providers remain the defaults. These local substitutes are opt-in:

| Capability | External/default | Credential-free validation |
| --- | --- | --- |
| RAG embeddings | `RAG_EMBEDDING_PROVIDER=openai` | `hash` |
| RAG reranking | `RAG_RERANKER_PROVIDER=cross_encoder` | `lexical` |
| Cache embeddings | `SEMANTIC_CACHE_EMBEDDING_PROVIDER=openai` | `hash` |
| Arbitration | `ARBITRATION_MODE=external` | `deterministic` |
| Secondary OCR | `OCR_EASYOCR_ENABLED=true` | `false` (Tesseract only) |
| Chat | provider-backed when configured | `COREMESH_CHAT_STUB=true` |

For a hermetic local or CI run, leave `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` blank and select every credential-free value above. Hash
embeddings are normalized, lexical reranking is token-overlap based, and
deterministic arbitration is repeatable. The log miner's `check` command needs
only PostgreSQL; a real mining run still needs its configured embedding and
reference-generation providers.

## Deploy with Compose

### Complete application

To build the runtime, gateway, and frontend and expose the dashboard at
<http://localhost:3000>:

~~~powershell
docker compose --profile app up --detach --build --wait
~~~

The browser targets <code>http://localhost:8080</code>; it never calls runtime
port 8000 directly. Gateway CORS defaults to local frontend ports 3000 and 5173,
and runtime trace artifacts persist in the <code>runtime-traces</code> volume.

### Stateful infrastructure only

~~~powershell
docker compose up --detach --wait
docker compose ps
~~~

This creates persistent named volumes and exposes development ports. The
PostgreSQL image runs <code>init.sql</code> only while initializing a new
volume. Re-running the non-idempotent script against an existing schema will
fail because the tables already exist.

## Host development

Compose is the supported integrated topology. The following service-local
commands are useful when developing one component against already-running
dependencies.

### Python runtime

~~~powershell
Set-Location services-runtime
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn src.main:app --reload --port 8000
~~~

The example environment file contains placeholders. Remove or replace example
API keys; an empty OpenAI key selects deterministic local extraction paths
where supported.

Check liveness:

~~~powershell
Invoke-RestMethod http://localhost:8000/health
~~~

See the [runtime guide](services-runtime/README.md) for ingestion examples,
native OCR requirements, configuration, and library-level usage.

### Go gateway

With Redis and the runtime available:

~~~powershell
Set-Location gateway-proxy
go run ./cmd
~~~

The gateway verifies Redis during startup and exits if it cannot connect. By
default both primary and fallback point to <code>http://localhost:8000</code>,
autopilot routing is enabled, and semantic caching enables itself only when an
OpenAI key is present unless explicitly configured otherwise.

Check the gateway itself:

~~~powershell
Invoke-RestMethod http://localhost:8080/healthz
~~~

See the [gateway guide](gateway-proxy/README.md) for every environment variable,
request/response header, cache rule, identity key, circuit transition, and
verification script.

### React frontend

With the gateway available:

~~~powershell
Set-Location frontend-ui
npm install
Copy-Item .env.example .env
npm run dev
~~~

Open <http://localhost:3000>. See the
[frontend guide](frontend-ui/README.md) for container, test, API-origin, and
browser-storage details.

## Analytics profile

Apply the idempotent migration before starting the scheduled profile:

~~~powershell
docker compose --profile analytics run --rm log-miner migrate
docker compose --profile analytics up -d log-miner
~~~

PostgreSQL runs `init.sql` only when initializing a new volume. That bootstrap
is not replay-safe. `analytics-workers/migrations/001_log_miner.sql` is the
idempotent upgrade path for both new and existing volumes and can safely be run
more than once.

Verify schema connectivity and the eligible-row count without returning any
prompt or response content:

~~~powershell
docker compose --profile analytics run --rm log-miner check
~~~

The runtime publisher remains disabled until
<code>PRODUCTION_INTERACTION_LOGGING_ENABLED=true</code> is set on the runtime.
Configure deployment-specific redaction with
<code>PRODUCTION_LOG_REDACTION_PATTERNS</code>; connection and statement timeout
settings bound its fail-open writes. The worker defaults to 02:00 UTC
and requires an OpenAI key only when a real mining run reaches
embedding/reference generation.

## Master end-to-end validation

From the repository root, run the integration test in Bash (Git Bash is
supported on Windows):

~~~bash
bash ./integration_test.sh
~~~

The script creates an isolated `coremesh-it-*` Compose project, generates a
random PostgreSQL password and eight free loopback ports, and ignores the
repository `.env`. It enables hash embeddings, lexical reranking,
deterministic arbitration, Tesseract-only OCR, stub chat, semantic caching,
forensics, and interaction logging. The running scenario calls no model APIs
and downloads no runtime model weights, though image builds and pulls may
still need network access.

Before testing requests, it starts fresh PostgreSQL state, applies the
log-miner migration twice, verifies bootstrap/upgrade catalog parity, and
idempotently seeds `cost_autopilot_routing` at 100% experimental rollout with
prompt version 2. The seven stages validate:

1. Gateway and frontend health, proxied runtime health, Redis-backed admission,
   and PostgreSQL-backed experiment routing.
2. Invoice ingestion with `index_for_rag=true` and a deterministic document ID.
3. Hybrid Qdrant/BM25 retrieval with matching metadata and both retrieval ranks.
4. Guardrailed SQL generation with a bounded PostgreSQL `SELECT` result.
5. The exact skipped-document workflow, deterministic score-2 blocking
   verdict, forensic trace, and eligible interaction-log row.
6. The content-free log-miner schema and eligible-row check.
7. A unique chat cache miss followed by a byte-identical hit and matching
   observability counters.

Failures print Compose state and the last 250 log lines. Cleanup removes only
the isolated test project, its volumes, and its temporary artifacts. Preserve
the stack and artifacts for inspection with:

~~~bash
KEEP_STACK=1 bash ./integration_test.sh
~~~

The script prints the preserved project name and URLs. Remove that exact
project when finished rather than running an unscoped cleanup command.

## HTTP surface

| Process | Method and path | Behavior |
| --- | --- | --- |
| Gateway | <code>GET /healthz</code> | Local liveness response; bypasses proxy middleware. |
| Gateway | <code>GET /v1/observability</code> | Gateway-start time, admission/cache/circuit configuration, and per-process traffic counters. |
| Runtime | <code>GET /health</code> | Runtime liveness response. |
| Runtime through gateway | <code>POST /v1/ingest</code> | OCR/extraction; multipart `index_for_rag=true` also indexes page chunks. |
| Runtime through gateway | <code>POST /v1/chat/completions</code> | OpenAI-shaped chat and the semantic-cache validation path. |
| Runtime through gateway | <code>POST /v1/execute</code> | Restricted RAG, text-to-SQL, or cue-based multi-agent orchestration request. |
| Runtime through gateway | <code>GET /v1/traces</code> | Filtered/paginated redacted forensic summaries. |
| Runtime through gateway | <code>GET /v1/traces/{trace_id}</code> | One validated redacted trace artifact. |

The browser uses only the gateway variants of runtime routes. Arbitration runs
inside orchestration; there is no direct arbitration or human-review API.

Ingestion remains extraction-only when `index_for_rag` is false or omitted.
When true, it returns a SHA-256 document ID and chunk count and indexes
nonempty page chunks into Qdrant and the application BM25 index. Re-ingesting
identical content replaces the same chunk IDs. After a runtime restart, BM25
rehydrates lazily from persisted Qdrant payloads.

`/v1/execute` is deliberately non-cacheable because it can run stateful RAG,
SQL, and agent workflows. Validate semantic caching through
`/v1/chat/completions`; repeated `/v1/execute` requests correctly bypass it.

## Test and verification commands

Run Python tests from <code>services-runtime</code> so imports and relative
paths match the service layout:

~~~powershell
Set-Location services-runtime
python -m pytest -q
python scripts/verify_ingestion.py
~~~

Run gateway unit tests from <code>gateway-proxy</code>:

~~~powershell
Set-Location gateway-proxy
go test ./...
~~~

Run frontend lint, unit tests, production build, and browser flows:

~~~powershell
Set-Location frontend-ui
npm run lint
npm test
npm run build
npx playwright install chromium
npm run test:e2e
~~~

Run analytics unit tests or the deterministic PostgreSQL smoke test from
<code>analytics-workers</code>:

~~~powershell
python -m pytest -q
python scripts/verify_log_miner.py
~~~

Run the offline self-healing documentation suite from the repository root:

~~~powershell
$env:PYTHONPATH = ".github/scripts"
python -B -m pytest -q -p no:cacheprovider .github/scripts/tests
~~~

The [self-healing package guide](.github/scripts/README.md) documents the CLI,
configuration, artifacts, and restricted-Windows test setup.

The gateway scripts exercise live routing and load behavior and require running
dependencies. Their usage and expected assertions are documented in
[gateway-proxy/scripts](gateway-proxy/scripts/README.md).

## Implementation status

| Capability | Status and integration |
| --- | --- |
| Gateway rate limiting and circuit breaking | Implemented and active for proxied requests. |
| Cost autopilot and experiment routing | Implemented; autopilot is enabled by default, PostgreSQL experiments are optional. |
| Semantic response cache | Implemented; optional and dependent on Redis Stack plus an embedding provider. |
| React operations dashboard | Implemented at port 3000 with execution, five-second gateway observability, and read-only interactive trace trees. |
| Document ingestion | Implemented and exposed at <code>/v1/ingest</code>. |
| Hybrid RAG | Implemented as a Python library and exposed through restricted <code>/v1/execute</code> RAG mode. |
| Guarded text-to-SQL | Implemented as a Python library and exposed through restricted <code>/v1/execute</code> SQL mode. |
| Agent orchestration and memory | Implemented as a Python library and exposed through restricted <code>/v1/execute</code> agent mode. |
| Consensus arbitration | Implemented inside agent execution; no direct HTTP or review-queue API. |
| Prompt registry and feature-flag packages | Database contracts or directories exist; application packages are placeholders. |
| Forensic tracing | Implemented as redacted OpenTelemetry JSON artifacts plus a SQLite registry and read-only list/detail APIs. |
| Production log mining | Implemented as an opt-in runtime publisher and daily HDBSCAN analytics worker. |
| Fine-tuning | Golden-data loading, PEFT/QLoRA training, W&B metrics, checkpoints, adapter export, and lineage manifests. |
| Regression and documentation CI | Model-regression CI is active; self-healing docs analyzes trusted structural PR changes and commits only independently validated bounded Markdown repairs. |

## Operations

Show profile-aware state and follow logs:

~~~bash
docker compose --profile app --profile analytics ps
docker compose --profile app --profile analytics logs --follow --tail=200
docker compose --profile app logs --follow runtime gateway
~~~

Check the gateway, its proxied runtime path, and the frontend:

~~~bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:3000/
~~~

The gateway container health check calls both local `/healthz` and proxied
`/health`, so it exercises gateway-to-runtime connectivity. Gateway startup
also performs bounded Redis and PostgreSQL checks. The runtime waits for
healthy PostgreSQL, Redis, and Qdrant.

Check a running worker or launch a one-shot mining run:

~~~bash
docker compose --profile analytics exec -T log-miner python -m src.log_miner.extractor check
docker compose --profile analytics run --rm log-miner run
~~~

Back up `postgres-data`, `qdrant-data`, `chroma-data`, `redis-data`, and
`runtime-traces` using each datastore's consistency requirements, and test
restoration. PostgreSQL owns application metadata and miner source rows;
Qdrant owns persisted RAG chunks; Chroma owns agent memory; Redis owns
admission/cache state; and the trace volume owns forensic artifacts.

### Safe and destructive shutdown

Pause containers while retaining containers, networks, and data:

~~~bash
docker compose --profile app --profile analytics stop
~~~

Remove containers and the project network while retaining named volumes:

~~~bash
docker compose --profile app --profile analytics down
~~~

The following is destructive and should be used only for an intentional clean
reset after verifying backups:

~~~bash
docker compose --profile app --profile analytics down --volumes --remove-orphans
~~~

It deletes PostgreSQL, Redis, Qdrant, Chroma, and forensic trace volumes for
the project. Without a backup, those contents are not recoverable.

## Troubleshooting

- **Compose requires `POSTGRES_PASSWORD`.** Copy `.env.example` to `.env`, set
  a nonblank value, then run `docker compose config --quiet`.
- **A new password is rejected on an old volume.** PostgreSQL applies image
  initialization credentials only when `postgres-data` is new. Change the role
  password inside PostgreSQL or restore into a deliberately recreated volume;
  do not casually delete the volume.
- **`/healthz` works but the gateway container is unhealthy.** Its full health
  check also proxies `/health`. Inspect runtime, Redis, PostgreSQL, and gateway
  logs for bounded startup-connection errors.
- **The miner reports a schema error.** Start PostgreSQL, run the idempotent
  migration, then rerun `check`. Do not mount `runtime-traces`; it is not the
  worker's input.
- **Indexed ingestion returns 422 or 503.** A 422 means OCR yielded no
  indexable page text. A 503 indicates an indexing dependency such as Qdrant
  is unavailable. Extraction-only ingestion remains available when indexing
  is false.
- **Credential-free mode calls providers or downloads weights.** Confirm both
  RAG providers, cache provider, deterministic arbitration,
  `OCR_EASYOCR_ENABLED=false`, `COREMESH_CHAT_STUB=true`, and blank keys. The
  integration script exports the complete set.
- **The semantic cache never hits.** Enable it, configure a valid embedding
  provider, send a stable identity such as `X-Team-ID`, and use
  `/v1/chat/completions`. `/v1/execute` always bypasses the cache.
- **The browser is blocked by CORS.** Add its exact origin to
  `GATEWAY_ALLOWED_ORIGINS`, align `VITE_GATEWAY_BASE_URL`, and rebuild the
  frontend image.
- **A host port is occupied.** Change the relevant `*_HOST_PORT` rather than
  binding a service to an untrusted interface.

## Production hardening

Before using CoreMesh with sensitive or multi-user data:

- Terminate TLS at a trusted ingress and add authenticated, authorized service
  and user identities. `X-Team-ID` is an accounting/cache identity, not
  authentication.
- Add tenant isolation to PostgreSQL, Redis keys, Qdrant collections, Chroma
  memory, traces, and every runtime execution path.
- Keep state services off public interfaces, restrict east-west traffic, and
  use least-privilege database roles. Give the text-to-SQL engine a read-only
  role limited to approved schemas.
- Store credentials in a secret manager instead of `.env`, rotate them,
  encrypt data and backups, and enforce tested retention/deletion policies.
- Define upload/request limits, container resource quotas, timeouts, egress
  policy, abuse controls, and deployment-specific PII redaction. Regex
  redaction is defense in depth, not a complete privacy boundary.
- Review model-provider data-use and residency terms and monitor cost before
  enabling external model, embedding, OCR, or telemetry endpoints.
- Pin images by immutable version or digest, scan dependencies, alert on
  service and disk health, and roll schema changes through tested backup,
  restore, and rollback procedures.

Treat documented headers and subsystem READMEs as module contracts. The
maintenance rules in [DOCUMENTATION.md](DOCUMENTATION.md) describe what must be
updated with future behavior changes.
