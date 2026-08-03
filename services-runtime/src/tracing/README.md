# Failure-forensics tracing

This package implements the Project 3 OpenTelemetry layer. Every agent
workflow receives a trace ID, emits a causal span tree, writes redacted JSON,
and stores a queryable summary in SQLite.

## Span boundaries

The workflow root contains repeated supervisor-node spans. Each specialist
contains a separate tool span, which contains model, OCR, retrieval, or
database spans. SQL, Qdrant, Chroma, Redis, semantic-memory, and short-term
memory boundaries are explicitly instrumented. Stable prefixes are
`coremesh.agent.*`, `coremesh.tool.*`, `coremesh.db.*`, and
`coremesh.model.*`.

Errors set OpenTelemetry `ERROR` status. Exception messages and stack frames
can contain customer content, so spans retain only exception type plus message
hash and length.

## Artifacts and registry

The default artifact is `.traces/<trace_id>.json`. It contains a flat span
list, nested visualization tree, trigger, metrics, and optional
`RootCauseDiagnosis`. Writes use an atomic replace. The SQLite
`trace_registry` indexes status, confidence, trigger, diagnosed span/step and
category, and artifact path. Negative feedback reanalyzes and upserts the row
when an artifact is available.

The runtime exposes newest-first summaries at <code>GET /v1/traces</code> and
one artifact at <code>GET /v1/traces/{trace_id}</code>. Summary filters cover
status, trigger, and diagnosed category with bounded limit/offset pagination.
Neither response exposes registry artifact paths, session hashes, prompts, or
responses. Trace IDs must be exactly 32 lowercase hexadecimal characters, so
the detail route cannot become a filesystem traversal boundary.

## Backward analysis

Execution errors and non-clean arbitration verdicts are analyzed
automatically. `ForensicsTracer.flag_negative_feedback(trace_id, reason)`
reanalyzes a stored artifact. The analyzer follows causal step order, removes
failed wrappers when a more specific descendant carries the same failure, and
reports the earliest degraded step before the healthy boundary.

Evidence includes span errors, failed invoice validation, unrecovered OCR
disagreement, confidence below `0.60`, and confidence drops of at least `0.20`.
An optional injected `StepQualityJudge` is used only when deterministic signals
are inconclusive and receives sanitized metadata only.

## Privacy, production feedback, export, and retention

Prompt, document, response, SQL, Redis-key, user-ID, feedback-reason, and raw
exception content are replaced by hashes and lengths. Categorical identifiers,
counts, timings, and quality metrics are allowlisted and bounded.

Tracing is fail-open for orchestration: exporter, storage, registry, and judge
failures cannot change an orchestration result. Forensic artifacts remain
until an operator removes them. Read-only HTTP listing/lookup reports
unavailable storage with a sanitized 503 instead of returning partial private
state.

A separate production-feedback sink is disabled by default. When enabled, it
stores only a regex-redacted prompt, prompt fingerprint, trace/feature scope,
and bounded arbitration signals in PostgreSQL. It never stores user IDs,
responses, or feedback reasons; later negative feedback flips only a boolean
for the trace. Sink failures are also fail-open. These source rows feed the
30-day analytics miner without changing the hash-only forensic artifacts.
The redacted prompt is published as soon as the workflow trace ID exists, then
updated with critic scores after arbitration so mid-flight failures remain
feedback-addressable. Connection establishment and each write have configurable
time limits. A feedback flag still succeeds when forensic JSON is disabled or
missing; root-cause reanalysis is simply omitted.

Setting standard `OTEL_EXPORTER_OTLP_ENDPOINT` adds batched OTLP export while
preserving local JSON. All settings are documented in `.env.example`.
