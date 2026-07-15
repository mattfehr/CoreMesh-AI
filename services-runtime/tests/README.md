# Runtime library tests

These tests protect implemented library contracts that are not all exposed by
FastAPI:

| File | Coverage |
| --- | --- |
| <code>test_retrieval.py</code> | Tokenization, stable IDs, fusion/rerank ordering, technical identifiers, and result provenance. |
| <code>test_sql_sandbox.py</code> | Statement/function blocking, limits, schema introspection, execution, and rollback. |
| <code>test_orchestrator.py</code> | Planning, specialist order, memory, partial failure, synthesis, and arbitration integration. |
| <code>test_consensus.py</code> | Quorum, retries, timeout, adjudication, remediation, and fail-closed verdicts. |
| <code>test_forensics.py</code> | OpenTelemetry trees, redaction, SQLite indexing, backward diagnosis, feedback, and deliberate sub-agent failure visualization. |

Run from <code>services-runtime</code>:

~~~powershell
python -m pytest -q
~~~

Tests use injected fakes, in-memory state, or SQLite and should not contact
OpenAI, Anthropic, Ollama, Redis, Qdrant, Chroma persistence, or PostgreSQL.
When a new external client is added, keep its constructor lazy and add an
injection seam so unit tests remain isolated.
