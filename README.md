# CoreMesh AI

CoreMesh is an AI engineering platform prototype that combines a Go edge
gateway with a Python intelligent runtime and local stateful infrastructure.
The repository implements several production-oriented building blocks while
retaining clearly marked placeholders for later roadmap phases.

The checked-in code and the directory READMEs are the source of truth for what
works today. The original [project blueprint](plan/coremesh.txt) describes a
larger target system and should not be read as an implementation-status report.

## What runs today

- The Go gateway on port 8080 provides Redis-backed token-bucket admission,
  primary/fallback reverse proxying, a concurrency-safe circuit breaker,
  request-complexity model routing, experiment splits, and an optional semantic
  response cache.
- The FastAPI runtime on port 8000 exposes liveness, document-ingestion, and a
  minimal OpenAI-shaped <code>/v1/chat/completions</code> path (deterministic
  stub unless live OpenAI is enabled). Ingestion performs image preprocessing,
  dual-engine OCR, optional vision fallback, structured extraction, and
  invoice-total validation.
- The runtime also contains tested library APIs for hybrid retrieval, guarded
  SQL, supervisor orchestration with memory, and multi-model consensus. Those
  libraries are not yet exposed as additional HTTP routes.
- Docker Compose starts PostgreSQL, Redis Stack, and Qdrant by default. An
  <code>app</code> profile boots runtime and gateway; an <code>analytics</code>
  profile adds the scheduled production log miner.
- Failure forensics and the opt-in production log feedback loop are
  implemented. Model-regression CI, guarded self-healing documentation, and
  feature-scoped PEFT/QLoRA training are live. Prompt/flag packages remain
  documented placeholders.

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed flows and state ownership.

## Runtime topology

~~~text
Client
  |
  v
Go gateway :8080
  |-- /healthz ------------------------------------> local response
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
  |
  +--> OCR engines / optional OpenAI calls

Shared and optional state:
  Redis Stack <---- rate limits and semantic cache
  PostgreSQL  <---- experiments, redacted interaction logs, eval datasets
  Qdrant      <---- hybrid-retrieval vectors
  Chroma      <---- long-term agent memory when orchestration is invoked

Optional analytics profile:
  Runtime -- redacted failures --> PostgreSQL --> daily HDBSCAN log miner
                                                --> golden_datasets / review
~~~

Middleware order matters. Requests enter autopilot first, then the optional
semantic cache, then rate limiting and proxy resilience. This lets complex
requests declare a cache bypass before cache lookup and ensures cache misses
still pass through admission control.

## Repository map

| Path | Responsibility |
| --- | --- |
| [gateway-proxy](gateway-proxy/README.md) | Go edge admission, routing, caching, and upstream resilience. |
| [services-runtime](services-runtime/README.md) | FastAPI ingestion service and intelligent-runtime libraries. |
| [analytics-workers](analytics-workers/README.md) | Scheduled log mining, model-regression evaluation, and PEFT/QLoRA adapter training. |
| [.github](.github/README.md) | Repository automation for model-regression gating and guarded self-healing documentation. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Cross-service flows, state, failure boundaries, and implementation status. |
| [DOCUMENTATION.md](DOCUMENTATION.md) | Required file headers, docstrings, comments, directory READMEs, and upkeep checklist. |
| <code>docker-compose.yml</code> | Local data services plus opt-in <code>app</code> and <code>analytics</code> profiles. |
| <code>init.sql</code> | First-boot PostgreSQL schema for prompts, experiments, redacted logs, candidates, and golden datasets. |

Each major source and test directory has its own README. Start there before
changing a subsystem; it records local invariants, dependencies, side effects,
and focused test commands.

## Quick start

### Prerequisites

- Docker Desktop with Compose
- Go 1.22 or newer
- Python 3.11 or newer
- Tesseract and Poppler for host-based document ingestion; the runtime
  Dockerfile installs the Linux packages
- An OpenAI API key only for LLM extraction, vision fallback, embeddings,
  OpenAI arbitration, semantic caching, production log labeling, or trusted
  self-healing documentation runs

EasyOCR and cross-encoder models can download weights on first use. Plan for
network access, startup time, and local cache space when invoking those paths.

### 1. Start stateful infrastructure

~~~powershell
docker compose up -d
docker compose ps
~~~

This creates persistent named volumes and exposes development ports. The
PostgreSQL image runs <code>init.sql</code> only while initializing a new
volume. Re-running the non-idempotent script against an existing schema will
fail because the tables already exist.

### 2. Start the Python runtime

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

### 3. Start the Go gateway

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

### 4. Enable the production log miner (optional)

Apply the idempotent migration before starting the scheduled profile:

~~~powershell
docker compose --profile analytics run --rm log-miner migrate
docker compose --profile analytics up -d log-miner
~~~

The runtime publisher remains disabled until
<code>PRODUCTION_INTERACTION_LOGGING_ENABLED=true</code> is set on the runtime.
Configure deployment-specific redaction with
<code>PRODUCTION_LOG_REDACTION_PATTERNS</code>; connection and statement timeout
settings bound its fail-open writes. The worker defaults to 02:00 UTC
and requires an OpenAI key only when a real mining run reaches
embedding/reference generation.

## HTTP surface

| Process | Method and path | Behavior |
| --- | --- | --- |
| Gateway | <code>GET /healthz</code> | Local liveness response; bypasses proxy middleware. |
| Runtime | <code>GET /health</code> | Runtime liveness response. |
| Runtime through gateway | <code>POST /v1/ingest</code> | Accepts PDF or supported raster multipart uploads and returns OCR/extraction/validation metadata. |

The RAG, SQL, orchestration, and arbitration modules currently expose Python
APIs, not HTTP endpoints.

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
| Document ingestion | Implemented and exposed at <code>/v1/ingest</code>. |
| Hybrid RAG | Implemented as a Python library; not mounted on FastAPI. |
| Guarded text-to-SQL | Implemented as a Python library; not mounted on FastAPI. |
| Agent orchestration and memory | Implemented as a Python library; not mounted on FastAPI. |
| Consensus arbitration | Implemented as a Python library; not mounted on FastAPI. |
| Prompt registry and feature-flag packages | Database contracts or directories exist; application packages are placeholders. |
| Forensic tracing | Implemented as redacted OpenTelemetry JSON artifacts plus a SQLite failure registry. |
| Production log mining | Implemented as an opt-in runtime publisher and daily HDBSCAN analytics worker. |
| Fine-tuning | Golden-data loading, PEFT/QLoRA training, W&B metrics, checkpoints, adapter export, and lineage manifests. |
| Regression and documentation CI | Model-regression CI is active; self-healing docs analyzes trusted structural PR changes and commits only independently validated bounded Markdown repairs. |

## Operational and security notes

This is a local-development stack, not a hardened deployment:

- Compose contains a visible development PostgreSQL password.
- The gateway and runtime implement no authentication or TLS termination.
- Some service ports bind to the host; inspect <code>docker-compose.yml</code>
  before using an untrusted network.
- Uploads are read into memory before OCR and may be sent to OpenAI when the
  corresponding key and fallback path are active.
- Redis stores rate-limit state and optional cached model responses. PostgreSQL,
  Qdrant, and Chroma can persist application data when their library paths are
  used. Enabled interaction logging stores regex-redacted prompts for up to 30
  days; regex redaction is defense in depth and must match deployment policy.
- Model/API calls can incur cost and transmit content to external providers.
  Self-healing documentation sends selected structural deltas, current
  Markdown blocks, and small neighboring style samples from trusted PRs to
  OpenAI; forks and Dependabot never enter that trust boundary.

Treat the documented headers and READMEs as part of each module contract. The
maintenance rules in [DOCUMENTATION.md](DOCUMENTATION.md) explain what must be
updated alongside future behavior changes.
