# CoreMesh intelligent runtime

This Python service owns document ingestion and provides reusable intelligent
runtime libraries for retrieval, guarded SQL, agent orchestration, memory, and
consensus arbitration. The checked-in FastAPI application exposes only
liveness and ingestion; the other capabilities require trusted Python callers.

## HTTP surface

| Method and path | Contract |
| --- | --- |
| <code>GET /health</code> | Process liveness only; no infrastructure check. |
| <code>POST /v1/ingest</code> | Multipart PDF/raster upload to typed invoice extraction and validation. |

The ingestion route accepts PDF, PNG, JPEG, TIFF, BMP, and WebP content types.
It reads the entire upload into memory, rejects empty/unsupported input, moves
synchronous OCR work to a worker thread, and maps processing failures to 422.

## Directory map

| Path | Responsibility |
| --- | --- |
| [src](src/README.md) | Runtime package and service entry point. |
| [src/ingestion](src/ingestion/README.md) | Implemented HTTP document pipeline. |
| [src/rag](src/rag/README.md) | Library-only dense/sparse retrieval. |
| [src/sql_engine](src/sql_engine/README.md) | Library-only read-only SQL boundary. |
| [src/agents](src/agents/README.md) | Library-only supervisor, specialists, and memory. |
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

The example API keys are placeholders. Delete the placeholder value for offline
ingestion or replace it with a real secret. Never commit <code>.env</code>.

Host-based PDF/OCR requires Tesseract and Poppler. The Dockerfile installs the
Linux packages, but <code>docker-compose.yml</code> does not start the runtime
image automatically.

## Configuration groups

<code>src.config.Settings</code> reads process variables and
<code>services-runtime/.env</code> during import. Restart after changes.
<code>.env.example</code> is the exhaustive commented list.

| Group | Important variables and consumers |
| --- | --- |
| OpenAI | API key plus extraction, vision, embedding, arbitration, and adjudicator models. An empty key selects offline ingestion but prevents default dense retrieval/OpenAI critics. |
| Other arbitration | Anthropic key/model, Ollama URL/model, score threshold, retry attempts, and overall timeout. |
| OCR | Disagreement threshold, optional Tesseract command, and optional Poppler path. |
| Infrastructure | PostgreSQL DSN for SQL, Redis URL for agent working memory, and Qdrant URL/collection/vector size for RAG. |
| Production feedback | Disabled-by-default publisher, JSON deployment-specific redaction patterns, and PostgreSQL connection/statement timeouts. It stores no user ID, response, or feedback reason. |
| Agent memory | Chroma directory/collection and Redis TTL. |
| Retrieval | Dense/sparse RRF weights, cross-encoder model, and exact technical-token priority. |
| Forensics | Enable flag, JSON/SQLite paths, confidence/drop thresholds, redacted attribute limit, and optional standard OTLP endpoint. |

Defaults target the root Compose stack. They are development defaults, not
production credentials or authorization boundaries.

## External dependencies and side effects

- Ingestion uses native Tesseract/Poppler, CPU/model-heavy EasyOCR/OpenCV, and
  optional OpenAI document transmission.
- RAG writes Qdrant, retains BM25 state in process, calls OpenAI embeddings, and
  can download/load a cross-encoder.
- SQL introspects and queries the configured database inside a rolled-back
  read-only transaction.
- Agents write expiring Redis state/events and persistent Chroma summaries;
  specialists can invoke all preceding side effects.
- Arbitration can transmit original prompts and synthesized responses to
  OpenAI, Anthropic, and Ollama, then block or replace output.
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

There is currently no test dedicated to the FastAPI liveness/media-type
boundary beyond the ingestion verification script.
