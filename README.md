# CoreMesh AI

Enterprise-grade AI Agent Platform for Corporate Knowledge and Automation.

## Architecture Overview

```
[ User / Client Application ]
          │
          ▼
┌─────────────────────────────────────────────────────┐
│ 1. GO GATEWAY PROXY LAYER (:8080)                   │
│   Prompt Registry · Traffic Splitter · Semantic     │
│   Cache · Cost Autopilot · Resiliency Engine        │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ 2. PYTHON RUNTIME LAYER (:8000)                     │
│   Document Ingestion · Hybrid RAG · LangGraph       │
│   Supervisor · Text-to-SQL · Output Arbitration     │
└───────────────────────┬─────────────────────────────┘
                        │
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
    PostgreSQL 16    Redis 7.2      Qdrant
    (metadata)       (cache)        (vectors)
```

## Repository Structure

```
.
├── .github/workflows/
│   ├── model-regression-ci.yml   # [Phase 4] Automated evaluation gates
│   └── self-healing-docs.yml     # [Phase 4] AST-driven documentation sync
├── gateway-proxy/                # Go 1.22 — traffic controller
│   ├── cmd/main.go
│   ├── internal/
│   │   ├── autopilot/            # [Phase 2] Complexity-based model routing
│   │   ├── cache/                # [Phase 2] Redis semantic memory proxy
│   │   ├── flags/                # [Phase 2] Feature flag evaluation
│   │   ├── gateway/              # [Phase 2] Token buckets & circuit breakers
│   │   └── registry/             # [Phase 2] Hot-reload prompt control
│   ├── go.mod
│   └── Dockerfile
├── services-runtime/             # Python 3.11 — async microservice layer
│   ├── src/
│   │   ├── ingestion/            # [Phase 1] Multi-modal OCR processors
│   │   ├── rag/                  # [Phase 1] Dense/sparse retrieval pipelines
│   │   ├── sql_engine/           # [Phase 1] Guardrailed text-to-SQL
│   │   ├── agents/               # [Phase 3] LangGraph supervisor workflows
│   │   ├── arbitration/          # [Phase 3] Parallel model critic pools
│   │   └── tracing/              # [Phase 3] Forensics trace listeners
│   ├── requirements.txt
│   └── Dockerfile
├── analytics-workers/            # Background automation infrastructure
│   └── src/
│       ├── log_miner/            # [Phase 4] HDBSCAN curation loop
│       └── fine_tuner/           # [Phase 4] PEFT/QLoRA training pipeline
├── docker-compose.yml
├── init.sql                      # PostgreSQL schema bootstrap
└── README.md
```

## Infrastructure Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Edge Proxy | Go 1.22 | Concurrent reverse proxy, rate limiting, circuit breaking |
| Runtime Engine | Python 3.11 + FastAPI | Async microservices, LangGraph agents |
| Cache / Vector Cache | Redis 7.2 (redis-stack) | Token buckets, semantic embedding cache (HNSW) |
| Metadata Store | PostgreSQL 16 | Prompt registry, experiments, golden datasets |
| Vector Index | Qdrant | Dense + sparse spatial document indices |

## Local Development — Quick Start

### Prerequisites
- Docker Desktop 4.x+
- Go 1.22+
- Python 3.11+

### 1. Start the data layer

```bash
docker compose up -d
```

This boots PostgreSQL 16, Redis 7.2 (redis-stack), and Qdrant. The `init.sql`
file is automatically executed by PostgreSQL on first launch, creating the
`prompt_registry`, `feature_experiments`, and `golden_datasets` tables.

### 2. Verify services

```bash
# PostgreSQL
docker exec coremesh-postgres pg_isready -U coremesh -d coremesh

# Redis
docker exec coremesh-redis redis-cli ping

# Qdrant
curl http://localhost:6333/healthz
```

### Service Ports

| Service | Port | Notes |
|---------|------|-------|
| PostgreSQL | 5432 | User: `coremesh` / Pass: `coremesh_secret` / DB: `coremesh` |
| Redis | 6379 | RedisInsight UI on 8001 |
| Qdrant REST | 6333 | |
| Qdrant gRPC | 6334 | |
| Gateway Proxy | 8080 | Go service (Phase 2) |
| Runtime API | 8000 | Python/FastAPI service (Phase 1) |

### 3. Run the Go gateway (Phase 2+)

```bash
cd gateway-proxy
go run ./cmd/main.go
```

### 4. Run the Python runtime (Phase 1+)

```bash
cd services-runtime
python -m venv .venv
.venv/Scripts/activate      # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000
```

## Implementation Phases

| Phase | Focus |
|-------|-------|
| **Phase 1** | Python Processing Engine — OCR ingestion, hybrid RAG, SQL guardrails |
| **Phase 2** | Go Gateway Layer — reverse proxy, rate limiting, semantic cache, cost autopilot |
| **Phase 3** | Agent Orchestration — LangGraph supervisor, consensus arbitration, forensic tracing |
| **Phase 4** | Continuous Optimization — log mining, regression CI, LoRA fine-tuning, self-healing docs |

## Environment Variables

Copy `.env.example` (to be created in Phase 1) to `.env` before running services.

Key variables:

```
OPENAI_API_KEY=
POSTGRES_DSN=postgresql://coremesh:coremesh_secret@localhost:5432/coremesh
REDIS_URL=redis://localhost:6379
QDRANT_URL=http://localhost:6333
```
