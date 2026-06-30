// Package main is the entry point for the CoreMesh AI Gateway Proxy.
//
// Phase 2 implementation will wire together:
//   - HTTP reverse proxy to the Python runtime layer (Project 11)
//   - Redis-backed token-bucket rate limiter (Project 11)
//   - Circuit breaker with automatic fallback routing (Project 11)
//   - Cosine-similarity semantic cache (Project 7)
//   - Cost autopilot complexity classifier (Project 2)
//   - Feature experiment traffic splitter (Project 9, 12)
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"

	"github.com/coremesh/gateway-proxy/internal/gateway"
)

func main() {
	cfg, err := gateway.ConfigFromEnv()
	if err != nil {
		log.Fatalf("gateway config error: %v", err)
	}

	proxyHandler, err := gateway.NewHandler(context.Background(), cfg)
	if err != nil {
		log.Fatalf("gateway startup error: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, `{"status":"ok","service":"coremesh-gateway"}`)
	})
	mux.Handle("/", proxyHandler)

	addr := ":8080"
	log.Printf("CoreMesh Gateway Proxy listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
