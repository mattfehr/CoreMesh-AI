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
category, and artifact path. Negative feedback reanalyzes and upserts the row.

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

## Privacy, export, and retention

Prompt, document, response, SQL, Redis-key, user-ID, feedback-reason, and raw
exception content are replaced by hashes and lengths. Categorical identifiers,
counts, timings, and quality metrics are allowlisted and bounded.

Tracing is fail-open: exporter, storage, registry, and judge failures cannot
change an orchestration result. Artifacts remain until an operator removes
them; automatic retention and feedback-to-eval generation are out of scope.
Setting standard `OTEL_EXPORTER_OTLP_ENDPOINT` adds batched OTLP export while
preserving local JSON. All local settings are documented in `.env.example`.
