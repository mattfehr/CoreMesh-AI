# Supervisor orchestration

This package implements a trusted Python API for deterministic task planning,
specialist execution, memory, synthesis, and response arbitration. It is not
mounted on FastAPI and does not implement human approval or a durable review
queue.

## Flow

~~~text
ExecutionRequestPayload
  -> retrieve similar Chroma memories
  -> heuristic ordered plan
  -> RAG / document / SQL specialists
  -> Redis state and event snapshots
  -> deterministic textual synthesis
  -> consensus arbitration (fail closed)
  -> Chroma completed-interaction summary
  -> JSON/SQLite forensic trace finalization
  -> OrchestrationResult
~~~

Query/context cues select steps. A request with no recognized cue defaults to
RAG. LangGraph runs the state machine when installed; a sequential adapter with
the same node contract is used otherwise.

## Specialists

- RAG invokes <code>HybridRetriever.search</code>.
- Document extraction accepts trusted context text, bytes, base64, or a
  filesystem path and invokes ingestion when decoding is needed.
- SQL introspects the database, chooses explicit SQL or a small heuristic
  SELECT generator, then invokes <code>SQLSandbox</code>.

The filesystem-path option assumes a trusted caller. Validate and constrain it
before exposing orchestration over a network.

## Memory and failure behavior

Redis stores JSON state and append-only events under session keys, normally
with a TTL. Chroma stores one deterministic-ID summary per completed session
using a local hash embedding. Both backends have in-memory test substitutes.

Specialist exceptions become failed observations and planning proceeds to the
next step. Redis and Chroma persistence failures are logged and degraded. Empty
or unarbitrated output is fail-closed: arbitration/runtime failure produces a
blocked response and status.

Every result includes a trace ID when forensics is enabled and an optional
root-cause diagnosis when execution or arbitration degraded. Trace persistence
is fail-open and stores hashes and metrics instead of request/response bodies.

Use <code>OrchestratorDependencies</code> to inject tools, stores, and an
arbitrator. Tests rely on that boundary; avoid hidden global clients in graph
nodes.
