# Supervisor orchestration

This package implements deterministic task planning, specialist execution,
memory, synthesis, and response arbitration. FastAPI mounts a restricted
<code>/v1/execute</code> projection for RAG, text-to-SQL, and agent mode. The
full Python API remains available to trusted callers and no human approval or
durable review queue is implemented.

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

The public RAG and SQL scopes force their corresponding single specialist.
Public agent mode and other trusted scopes retain cue-based multi-step
planning; a request with no recognized cue defaults to RAG. LangGraph runs the
state machine when installed; a sequential adapter with the same node contract
is used otherwise.

## Specialists

- RAG invokes <code>HybridRetriever.search</code>.
- Document extraction accepts trusted context text, bytes, base64, or a
  filesystem path and invokes ingestion when decoding is needed. When none is
  supplied, it emits an explicit <code>skipped</code> observation.
- SQL introspects the database, chooses explicit SQL or a small heuristic
  SELECT generator, then invokes <code>SQLSandbox</code>.

The filesystem-path option assumes a trusted caller. The mounted HTTP model
forbids it, document bytes/text, explicit SQL, and every unknown context field.

## Memory and failure behavior

Redis stores JSON state and append-only events under session keys, normally
with a TTL. Chroma stores one deterministic-ID summary per completed session
using a local hash embedding. Both backends have in-memory test substitutes.

<code>ToolObservation.status</code> is <code>success</code>, <code>error</code>,
or <code>skipped</code>. Specialist exceptions become failed observations and
produce <code>completed_with_errors</code> before arbitration. Inapplicable
steps become skipped observations and produce <code>completed_with_gaps</code>;
synthesis records the skip reason rather than a synthetic failure. Planning
continues in either case. Redis and Chroma persistence failures are logged and
degraded. Empty or unarbitrated output is fail-closed: arbitration/runtime
failure produces a blocked response and status.

Arbitration receives workflow status and failed/skipped observation counts,
not specialist content, in its metadata. <code>ARBITRATION_MODE</code> selects
the external or deterministic dependency factory described in the arbitration
package.

Every result includes a trace ID when forensics is enabled and an optional
root-cause diagnosis when execution or arbitration degraded. Trace persistence
is fail-open and stores hashes and metrics instead of request/response bodies.

Use <code>OrchestratorDependencies</code> to inject tools, stores, and an
arbitrator. Tests rely on that boundary; avoid hidden global clients in graph
nodes.
