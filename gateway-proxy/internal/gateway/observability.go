// Package gateway exposes a browser-safe operational snapshot around the edge proxy.
//
// This file owns per-process request counters, the local observability route,
// and the explicit CORS allowlist used by the separately served React UI.
// Counters reset on process restart and never read or expose request bodies.
package gateway

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/coremesh/gateway-proxy/internal/cache"
)

const observabilityPath = "/v1/observability"

var exposedGatewayHeaders = []string{
	"Retry-After",
	"X-RateLimit-Remaining",
	"X-CoreMesh-Cache",
	"X-CoreMesh-Circuit-State",
	"X-CoreMesh-Route",
	"X-CoreMesh-Autopilot-Tier",
	"X-CoreMesh-Routed-Model",
	"X-CoreMesh-Autopilot-Reason",
	"X-CoreMesh-Experiment-Variant",
	"X-CoreMesh-Prompt-Version",
}

// RateLimitSnapshot describes the configured distributed admission budget.
type RateLimitSnapshot struct {
	Capacity        int64   `json:"capacity"`
	RefillPerSecond float64 `json:"refill_per_second"`
}

// CacheSnapshot contains semantic-cache outcomes observed by this process.
type CacheSnapshot struct {
	Enabled  bool     `json:"enabled"`
	Hits     uint64   `json:"hits"`
	Misses   uint64   `json:"misses"`
	Bypasses uint64   `json:"bypasses"`
	HitRate  *float64 `json:"hit_rate"`
}

// CircuitSnapshot describes current and configured breaker behavior.
type CircuitSnapshot struct {
	State                string  `json:"state"`
	FailureThreshold     int     `json:"failure_threshold"`
	FailureWindowSeconds float64 `json:"failure_window_seconds"`
	OpenDurationSeconds  float64 `json:"open_duration_seconds"`
}

// TrafficSnapshot contains bounded, content-free request outcome counters.
type TrafficSnapshot struct {
	Requests       uint64 `json:"requests"`
	Primary        uint64 `json:"primary"`
	Fallback       uint64 `json:"fallback"`
	RateLimited    uint64 `json:"rate_limited"`
	UpstreamErrors uint64 `json:"upstream_errors"`
}

// ObservabilitySnapshot is the stable JSON contract consumed by frontend-ui.
type ObservabilitySnapshot struct {
	GeneratedAt    time.Time         `json:"generated_at"`
	StartedAt      time.Time         `json:"started_at"`
	RateLimit      RateLimitSnapshot `json:"rate_limit"`
	SemanticCache  CacheSnapshot     `json:"semantic_cache"`
	CircuitBreaker CircuitSnapshot   `json:"circuit_breaker"`
	Traffic        TrafficSnapshot   `json:"traffic"`
}

type circuitStateReader interface {
	CircuitState() string
}

type gatewayMetrics struct {
	startedAt      time.Time
	cacheEnabled   bool
	requests       atomic.Uint64
	cacheHits      atomic.Uint64
	cacheMisses    atomic.Uint64
	cacheBypasses  atomic.Uint64
	primary        atomic.Uint64
	fallback       atomic.Uint64
	rateLimited    atomic.Uint64
	upstreamErrors atomic.Uint64
}

func newGatewayMetrics(cacheEnabled bool) *gatewayMetrics {
	return &gatewayMetrics{
		startedAt:    time.Now().UTC(),
		cacheEnabled: cacheEnabled,
	}
}

// Middleware records response metadata without buffering response bodies.
func (m *gatewayMetrics) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		observed := &observedResponseWriter{ResponseWriter: w}
		next.ServeHTTP(observed, r)
		m.record(observed.statusCode(), observed.Header())
	})
}

func (m *gatewayMetrics) record(statusCode int, header http.Header) {
	m.requests.Add(1)
	switch strings.ToLower(strings.TrimSpace(header.Get(cache.HeaderCache))) {
	case "hit":
		m.cacheHits.Add(1)
	case "miss":
		m.cacheMisses.Add(1)
	case "bypass":
		m.cacheBypasses.Add(1)
	}
	switch strings.ToLower(strings.TrimSpace(header.Get(coremeshRouteHeader))) {
	case "primary":
		m.primary.Add(1)
	case "fallback":
		m.fallback.Add(1)
	}
	if statusCode == http.StatusTooManyRequests {
		m.rateLimited.Add(1)
	}
	if statusCode >= http.StatusInternalServerError {
		m.upstreamErrors.Add(1)
	}
}

func (m *gatewayMetrics) snapshot(cfg Config, circuit circuitStateReader) ObservabilitySnapshot {
	hits := m.cacheHits.Load()
	misses := m.cacheMisses.Load()
	var hitRate *float64
	if eligible := hits + misses; eligible > 0 {
		value := float64(hits) / float64(eligible)
		hitRate = &value
	}
	return ObservabilitySnapshot{
		GeneratedAt: time.Now().UTC(),
		StartedAt:   m.startedAt,
		RateLimit: RateLimitSnapshot{
			Capacity:        cfg.RateLimitCapacity,
			RefillPerSecond: cfg.RateLimitRefillPerSecond,
		},
		SemanticCache: CacheSnapshot{
			Enabled:  m.cacheEnabled,
			Hits:     hits,
			Misses:   misses,
			Bypasses: m.cacheBypasses.Load(),
			HitRate:  hitRate,
		},
		CircuitBreaker: CircuitSnapshot{
			State:                circuit.CircuitState(),
			FailureThreshold:     cfg.CircuitFailureThreshold,
			FailureWindowSeconds: cfg.CircuitFailureWindow.Seconds(),
			OpenDurationSeconds:  cfg.CircuitOpenDuration.Seconds(),
		},
		Traffic: TrafficSnapshot{
			Requests:       m.requests.Load(),
			Primary:        m.primary.Load(),
			Fallback:       m.fallback.Load(),
			RateLimited:    m.rateLimited.Load(),
			UpstreamErrors: m.upstreamErrors.Load(),
		},
	}
}

type applicationHandler struct {
	next           http.Handler
	circuit        circuitStateReader
	cfg            Config
	metrics        *gatewayMetrics
	allowedOrigins map[string]struct{}
}

func newApplicationHandler(
	next http.Handler,
	circuit circuitStateReader,
	cfg Config,
	metrics *gatewayMetrics,
) http.Handler {
	origins := make(map[string]struct{}, len(cfg.AllowedOrigins))
	for _, origin := range cfg.AllowedOrigins {
		origins[origin] = struct{}{}
	}
	return &applicationHandler{
		next:           next,
		circuit:        circuit,
		cfg:            cfg,
		metrics:        metrics,
		allowedOrigins: origins,
	}
}

func (h *applicationHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if h.applyCORS(w, r) {
		return
	}
	if r.URL.Path == observabilityPath {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", http.MethodGet)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(w).Encode(h.metrics.snapshot(h.cfg, h.circuit)); err != nil {
			http.Error(w, "failed to encode observability snapshot", http.StatusInternalServerError)
		}
		return
	}
	h.next.ServeHTTP(w, r)
}

// applyCORS writes allowlisted browser headers and handles preflight locally.
func (h *applicationHandler) applyCORS(w http.ResponseWriter, r *http.Request) bool {
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	_, allowed := h.allowedOrigins[origin]
	if origin != "" && allowed {
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Access-Control-Expose-Headers", strings.Join(exposedGatewayHeaders, ", "))
		w.Header().Add("Vary", "Origin")
	}

	if r.Method != http.MethodOptions ||
		strings.TrimSpace(r.Header.Get("Access-Control-Request-Method")) == "" {
		return false
	}
	if origin == "" || !allowed {
		http.Error(w, "origin not allowed", http.StatusForbidden)
		return true
	}
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	w.Header().Set(
		"Access-Control-Allow-Headers",
		"Accept, Content-Type, X-Team-ID, X-API-Key, X-CoreMesh-Cache-Policy",
	)
	w.Header().Set("Access-Control-Max-Age", "600")
	w.Header().Add("Vary", "Access-Control-Request-Method")
	w.Header().Add("Vary", "Access-Control-Request-Headers")
	w.WriteHeader(http.StatusNoContent)
	return true
}

// observedResponseWriter preserves streaming/hijacking interfaces while recording status.
type observedResponseWriter struct {
	http.ResponseWriter
	status int
}

func (w *observedResponseWriter) WriteHeader(statusCode int) {
	if w.status != 0 {
		return
	}
	w.status = statusCode
	w.ResponseWriter.WriteHeader(statusCode)
}

func (w *observedResponseWriter) Write(body []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(body)
}

func (w *observedResponseWriter) statusCode() int {
	if w.status == 0 {
		return http.StatusOK
	}
	return w.status
}

func (w *observedResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (w *observedResponseWriter) Flush() {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *observedResponseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	hijacker, ok := w.ResponseWriter.(http.Hijacker)
	if !ok {
		return nil, nil, fmt.Errorf("response writer does not support hijacking")
	}
	return hijacker.Hijack()
}

func (w *observedResponseWriter) Push(target string, options *http.PushOptions) error {
	if pusher, ok := w.ResponseWriter.(http.Pusher); ok {
		return pusher.Push(target, options)
	}
	return http.ErrNotSupported
}

func (w *observedResponseWriter) ReadFrom(reader io.Reader) (int64, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	if readerFrom, ok := w.ResponseWriter.(io.ReaderFrom); ok {
		return readerFrom.ReadFrom(reader)
	}
	return io.Copy(struct{ io.Writer }{w.ResponseWriter}, reader)
}
