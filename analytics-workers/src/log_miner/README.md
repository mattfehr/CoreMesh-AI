# Production log miner

## Responsibility

This package implements the Phase 4.1 background curation loop. It owns log
selection, embedding validation, HDBSCAN clustering, representative selection,
structured reference generation, review routing, idempotent golden promotion,
run auditing, retention cleanup, and daily scheduling.

It does not collect raw traffic, expose a review UI, run model regressions, or
implement local provider adapters. Runtime collection remains an explicit,
disabled-by-default integration in <code>services-runtime</code>.

## Contracts and invariants

- Inputs contain redacted prompt text only and qualify on negative feedback or
  <code>min_arbitration_score &lt; 4</code>.
- Clustering never mixes feature scopes. Embedding dimensions and finite
  values are validated before L2 normalization.
- HDBSCAN defaults are minimum cluster size 3, minimum samples 2, Euclidean
  distance, and EOM selection.
- Each cluster uses its medoid; noise produces at most 20 singleton candidates
  per feature/run.
- Outputs contain a reference answer, validation criteria, expected behavior,
  failure pattern, difficulty, and label confidence.
- Confidence of 0.80 or more promotes in the same transaction as candidate
  persistence. Lower confidence is review-only.
- SHA-256 over schema version, feature scope, and normalized representative
  prompt is the stable idempotency key.

## Providers and external effects

OpenAI is the default provider: <code>text-embedding-3-small</code> supplies
batch vectors and configurable GPT-4o supplies structured references. Both are
injectable, so unit and deterministic verification paths make no external
calls.

The PostgreSQL repository reads the complete rolling window, uses a renewable
lease and fencing token for crash-safe ownership, caches prompt/model vectors,
records normalized cluster membership, and atomically finalizes run audit,
retention, and lease release. Provider calls hold no database connection.
Statements are parameterized and promotion is transactional.

## Operations

The CLI commands are <code>migrate</code>, <code>run</code>, and
<code>schedule</code>. A failed one-shot run exits nonzero and records a
sanitized summary when PostgreSQL is available. The scheduler logs job failure
and remains alive for the next cron occurrence; normal termination signals stop
it gracefully.
