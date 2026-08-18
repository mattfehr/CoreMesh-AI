# Cost autopilot

Autopilot is the outer request middleware. It recognizes JSON POST payloads
with a prompt, input, messages, or unified-execution `payload_query`, computes
a deterministic complexity classification, and exposes the decision through
request/response headers. OpenAI-shaped requests have their model field
rewritten. `/v1/execute` is classify-only so its strict runtime payload remains
unchanged. Non-JSON, non-POST, empty, malformed, or promptless requests pass
through unchanged.

## Classification

The score considers approximate prompt length, code/error markers, reasoning
keywords, constraint count, tools/functions, complex response format, large
token budget, and high temperature. Score 3 or higher selects tier 3 and
bypasses semantic caching; lower scores select tier 1 and allow caching.

These thresholds are policy contracts protected by tests. When changing one,
update routing examples, cache expectations, and cost/quality assumptions
together.

## Experiments

When <code>POSTGRES_DSN</code> is set, construction performs a bounded
PostgreSQL ping before the gateway starts, then the store reads one row from
<code>feature_experiments</code> with the same configured per-request timeout.
A SHA-256 hash of flag plus preferred user identity assigns a stable bucket
from 0 to 99.

- running experiments choose experimental inside rollout and baseline outside;
- rolled-back experiments force baseline;
- other/missing statuses keep the classifier decision;
- lookup errors fail toward tier-3 baseline, optionally exposing a sanitized
  debug header.

The package reads experiment state only. It has no prompt registry or mutation
API. Close the PostgreSQL pool through its store lifecycle when embedding this
package outside the current process composition.
