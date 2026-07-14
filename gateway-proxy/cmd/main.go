// Package main assembles and starts the CoreMesh edge gateway.
//
// System role: it owns process startup, the local health endpoint, and the
// catch-all handoff to gateway middleware.
// Dependencies: internal/gateway constructs Redis admission, optional routing
// and cache middleware, and primary/fallback reverse proxies.
// Side effects: startup reads environment variables, connects to Redis and
// optional providers/stores, opens port 8080, and exits on fatal errors.
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
