module github.com/coremesh/gateway-proxy

go 1.22

// Dependencies are added in Phase 2 when gateway application code is implemented.
// Expected additions:
//   github.com/redis/go-redis/v9       — Redis token bucket & semantic cache
//   github.com/sony/gobreaker           — Circuit breaker
//   go.opentelemetry.io/otel           — Distributed tracing
