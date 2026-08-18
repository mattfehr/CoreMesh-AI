# Edge admission and resilience

This package owns the mandatory gateway core, browser CORS, operational
counters, and optional middleware composition.
Its public construction paths separate production dependencies from testable
logic:

- <code>NewHandler</code> parses Redis, pings it, builds the token bucket,
  optional cache/autopilot layers, performs a bounded PostgreSQL ping when an
  experiment DSN is configured, and adds the local observability endpoint and
  CORS wrapper.
- <code>NewProxy</code> accepts an injected <code>RateLimiter</code> for
  deterministic unit tests.

## Token bucket

One Redis hash per identity stores fractional tokens and the last Redis-server
timestamp. A Lua script refills and consumes atomically, so multiple gateway
instances share a consistent bucket without trusting host clocks. Keys expire
after at least 60 seconds and normally after twice the full-refill interval.

Limiter errors return 503 rather than bypassing budget enforcement. Rejected
requests receive 429, remaining capacity, and a rounded-up retry delay.

## Circuit breaker

Primary transport errors and 5xx responses count inside a rolling window.
Closed traffic uses primary. Open traffic uses fallback. After the open
duration, one request becomes the half-open primary probe while concurrent
requests stay on fallback. Probe success closes and clears failures; probe
failure reopens.

Request bodies are copied into replayable memory before primary proxying so the
threshold-crossing request can use fallback after a primary error. Fallback
outcomes do not change primary circuit state.

## Browser and observability boundary

An exact origin allowlist admits the local frontend on ports 3000 and 5173 by
default. Preflights terminate outside proxy/admission middleware. Allowed
responses expose rate-limit, cache, circuit, route, and autopilot headers.

The local <code>GET /v1/observability</code> endpoint returns gateway-start
time, admission/circuit configuration, cache outcome arithmetic, and
content-free traffic counters. Atomic counters wrap the complete proxy
middleware chain but exclude this endpoint and preflights. They are
per-process and intentionally have no durable store.

## Invariants

- Preserve the mutex around every transition and half-open lease.
- Do not count normal 4xx primary responses as provider failures.
- Reset the buffered body before fallback.
- Preserve original host in <code>X-Forwarded-Host</code> while targeting the
  configured upstream host.
- Keep rate limiting ahead of upstream selection.
- Keep CORS preflight and observability outside admission and metrics.
- Never add request bodies, prompts, identities, or artifact paths to the
  observability contract.

Tests in this directory cover concurrent probes, rolling-window pruning, body
replay, headers, proxy errors, miniredis token-bucket concurrency, atomic
counter arithmetic, circuit snapshots, and CORS allow/deny behavior.
