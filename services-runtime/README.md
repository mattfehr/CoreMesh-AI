# CoreMesh intelligent runtime

This Python service owns document ingestion, a minimal OpenAI-shaped chat path,
unified RAG/SQL/agent execution, and read-only forensic trace APIs. The public
execution boundary exposes a deliberately restricted projection of the
reusable orchestration libraries.

## HTTP surface

| Method and path | Contract |
| --- | --- |
| <code>GET /health</code> | Process liveness only; no infrastructure check. |
| <code>POST /v1/ingest</code> | Multipart PDF/raster extraction with optional page-level hybrid-RAG indexing. |
| <code>POST /v1/chat/completions</code> | OpenAI-shaped chat body; deterministic stub unless live OpenAI is enabled. |
| <code>POST /v1/execute</code> | Run <code>rag</code>, <code>text_to_sql</code>, or <code>agent_orchestrator</code> with application-scoped injectable dependencies on a worker thread. |
| <code>GET /v1/traces</code> | Newest-first filtered/paginated redacted forensic summaries. |
| <code>GET /v1/traces/{trace_id}</code> | One validated redacted forensic artifact; missing IDs return 404. |

The execute route accepts only <code>user_id</code>,
<code>feature_scope</code>, <code>payload_query</code>, and a session context
containing optional <code>session_id</code> and <code>rag_top_k</code> from 1
through 20. Unknown context fields—including paths, document content, or SQL
overrides—return 422. Synchronous orchestration runs in a worker thread.
Expected specialist/arbitration failures remain structured results; unexpected
boundary failures return a sanitized 502.

Chat completions return a stable stub when <code>COREMESH_CHAT_STUB=true</code>
or <code>OPENAI_API_KEY</code> is unset. With a key and stub disabled, the
runtime forwards to OpenAI.

The ingestion route accepts PDF, PNG, JPEG, TIFF, BMP, and WebP content types.
It reads the entire upload into memory, rejects empty/unsupported input, and
moves synchronous OCR work to a worker thread. The multipart field
<code>index_for_rag</code> defaults to <code>false</code>, preserving the
extraction-only response and omitting <code>rag_index</code>. When true, the
runtime derives a SHA-256 document ID from the upload, indexes each non-empty
OCR page through the application-scoped retriever, and returns the document ID
and chunk count. Processing failures and documents with no indexable text
return 422; unavailable embedding or Qdrant dependencies return a sanitized
503.

## Directory map

| Path | Responsibility |
| --- | --- |
| [src](src/README.md) | Runtime package and service entry point. |
| [src/ingestion](src/ingestion/README.md) | Implemented HTTP document pipeline. |
| [src/rag](src/rag/README.md) | Dense/sparse retrieval library, reachable through restricted execute mode. |
| [src/sql_engine](src/sql_engine/README.md) | Read-only SQL boundary, reachable through restricted execute mode. |
| [src/agents](src/agents/README.md) | Supervisor, specialists, memory, and unified execute implementation. |
| [src/arbitration](src/arbitration/README.md) | Library-only multi-provider delivery gate. |
| [src/tracing](src/tracing/README.md) | OpenTelemetry trees, JSON/SQLite registry, and backward failure analysis. |
| [scripts](scripts/README.md) | In-process ingestion verification. |
| [tests](tests/README.md) | Isolated unit/contract tests for library subsystems. |
| [fixtures](fixtures/README.md) | Generated synthetic invoice test asset. |

## Local setup

From this directory:

~~~powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn src.main:app --reload --port 8000
~~~

The example keeps secrets blank. Leave them blank for hermetic modes or supply
deployment-managed credentials when deliberately enabling external providers.
Never commit <code>.env</code>.

Host-based PDF/OCR requires Tesseract and Poppler. The Dockerfile installs the
Linux packages. The default Compose stack omits application processes; use the
<code>app</code> profile to start runtime, gateway, and frontend together.

## Configuration groups

<code>src.config.Settings</code> reads process variables and
<code>services-runtime/.env</code> during import. Restart after changes.
<code>.env.example</code> is the exhaustive commented list.

| Group | Important variables and consumers |
| --- | --- |
| OpenAI | API key plus extraction, vision, embedding, arbitration, and adjudicator models. An empty key selects offline extraction and requires the hash provider for dense retrieval. |
| Arbitration | <code>ARBITRATION_MODE</code>, Anthropic key/model, Ollama URL/model, score threshold, retry attempts, and overall timeout. External mode is the default. |
| OCR | Disagreement threshold, <code>OCR_EASYOCR_ENABLED</code>, optional Tesseract command, and optional Poppler path. |
| Infrastructure | PostgreSQL DSN for SQL, Redis URL for agent working memory, and Qdrant URL/collection/vector size for RAG. |
| Production feedback | Disabled-by-default publisher, JSON deployment-specific redaction patterns, and PostgreSQL connection/statement timeouts. It stores no user ID, response, or feedback reason. |
| Agent memory | Chroma directory/collection and Redis TTL. |
| Retrieval | <code>RAG_EMBEDDING_PROVIDER</code>, <code>RAG_RERANKER_PROVIDER</code>, dense/sparse RRF weights, cross-encoder model, and exact technical-token priority. |
| Forensics | Enable flag, JSON/SQLite paths, confidence/drop thresholds, redacted attribute limit, and optional standard OTLP endpoint. Compose persists JSON/SQLite under the <code>runtime-traces</code> volume. |

Defaults target the root Compose stack. They are development defaults, not
production credentials or authorization boundaries.

## External dependencies and side effects

- Ingestion uses native Tesseract/Poppler and OpenCV. EasyOCR and OpenAI
  document transmission are selectable and disabled in hermetic validation.
- RAG persists text and metadata payloads in Qdrant. It lazily rebuilds the
  application-scoped BM25 index from those payloads after restart. The default
  providers call OpenAI and can download a cross-encoder; hash embeddings plus
  lexical reranking avoid both external model calls and model downloads.
- SQL introspects and queries the configured database inside a rolled-back
  read-only transaction.
- Agents write expiring Redis state/events and persistent Chroma summaries;
  specialists can invoke all preceding side effects.
- External arbitration can transmit original prompts and synthesized responses
  to OpenAI, Anthropic, and Ollama, then block or replace output. Deterministic
  mode consumes categorical workflow metadata only and makes no provider call.
- The opt-in production-feedback publisher writes regex-redacted prompts and
  bounded arbitration signals to PostgreSQL. Its writes and later feedback
  flag updates are fail-open and bounded by connection/statement timeouts.

Heavy clients are generally lazy. Importing the FastAPI app does not connect to
Qdrant, Redis, Chroma, PostgreSQL, or model providers.

## Verification

~~~powershell
python -m pytest -q
python scripts/verify_ingestion.py
~~~

The unit tests use fakes or in-memory databases and require no live provider or
Compose service. The ingestion verification rewrites the fixture and runs
native OCR; it can call OpenAI if a key is configured.

HTTP contract tests cover opt-in ingestion indexing and its 422/503 boundaries,
execution validation/delegation, forensic pagination, path-safe trace IDs,
missing artifacts, and redaction.
