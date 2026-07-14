// Module: dependency contract for the CoreMesh Go gateway.
// Role: pins the packages used by edge admission, routing, caching, and tests.
// Dependencies: interpreted by Go 1.22 module tooling together with go.sum.
// Side effects: Go commands may download modules into the developer/build cache.

module github.com/coremesh/gateway-proxy

go 1.22

require (
	github.com/alicebob/miniredis/v2 v2.35.0
	github.com/jackc/pgx/v5 v5.5.5
	github.com/redis/go-redis/v9 v9.18.0
)

require (
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/dgryski/go-rendezvous v0.0.0-20200823014737-9f7001d12a5f // indirect
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20221227161230-091c0ba34f0a // indirect
	github.com/jackc/puddle/v2 v2.2.1 // indirect
	github.com/yuin/gopher-lua v1.1.1 // indirect
	go.uber.org/atomic v1.11.0 // indirect
	golang.org/x/crypto v0.17.0 // indirect
	golang.org/x/sync v0.1.0 // indirect
	golang.org/x/text v0.14.0 // indirect
)
