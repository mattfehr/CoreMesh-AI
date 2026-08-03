// Module: gateway observability and browser CORS contract tests.
// System role: protects content-free counters, local snapshot routing, and
// allowlisted cross-origin behavior for frontend-ui.
// Dependencies: Go testing plus process-local HTTP recorders.
// Side effects: none outside process memory.
package gateway

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/coremesh/gateway-proxy/internal/cache"
)

type fixedCircuitState string

func (state fixedCircuitState) CircuitState() string {
	return string(state)
}

func TestObservabilitySnapshotCountsTrafficAndExcludesItself(t *testing.T) {
	cfg := DefaultConfig()
	cfg.AllowedOrigins = []string{"http://localhost:3000"}
	metrics := newGatewayMetrics(true)
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/hit":
			w.Header().Set(cache.HeaderCache, "hit")
			w.Header().Set(coremeshRouteHeader, "primary")
		case "/miss":
			w.Header().Set(cache.HeaderCache, "miss")
			w.Header().Set(coremeshRouteHeader, "fallback")
		case "/limited":
			w.Header().Set(cache.HeaderCache, "bypass")
			w.WriteHeader(http.StatusTooManyRequests)
		case "/error":
			w.WriteHeader(http.StatusBadGateway)
		}
	})
	handler := newApplicationHandler(
		metrics.Middleware(next),
		fixedCircuitState("open"),
		cfg,
		metrics,
	)

	for _, path := range []string{"/hit", "/miss", "/limited", "/error"} {
		request := httptest.NewRequest(http.MethodGet, path, nil)
		request.Header.Set("Origin", "http://localhost:3000")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if got := response.Header().Get("Access-Control-Allow-Origin"); got == "" {
			t.Fatalf("%s missing allowed origin", path)
		}
	}

	request := httptest.NewRequest(http.MethodGet, observabilityPath, nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	var snapshot ObservabilitySnapshot
	if err := json.NewDecoder(response.Body).Decode(&snapshot); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if snapshot.Traffic.Requests != 4 {
		t.Fatalf("requests = %d, want 4", snapshot.Traffic.Requests)
	}
	if snapshot.Traffic.Primary != 1 || snapshot.Traffic.Fallback != 1 {
		t.Fatalf("routes = primary:%d fallback:%d, want 1/1",
			snapshot.Traffic.Primary, snapshot.Traffic.Fallback)
	}
	if snapshot.Traffic.RateLimited != 1 || snapshot.Traffic.UpstreamErrors != 1 {
		t.Fatalf("outcomes = rate-limited:%d errors:%d, want 1/1",
			snapshot.Traffic.RateLimited, snapshot.Traffic.UpstreamErrors)
	}
	if snapshot.SemanticCache.Hits != 1 || snapshot.SemanticCache.Misses != 1 ||
		snapshot.SemanticCache.Bypasses != 1 {
		t.Fatalf("cache snapshot = %#v", snapshot.SemanticCache)
	}
	if snapshot.SemanticCache.HitRate == nil || *snapshot.SemanticCache.HitRate != 0.5 {
		t.Fatalf("hit rate = %v, want 0.5", snapshot.SemanticCache.HitRate)
	}
	if snapshot.CircuitBreaker.State != "open" {
		t.Fatalf("circuit state = %q, want open", snapshot.CircuitBreaker.State)
	}
}

func TestCORSPreflightIsAllowlistedAndDoesNotReachProxy(t *testing.T) {
	cfg := DefaultConfig()
	metrics := newGatewayMetrics(false)
	hits := 0
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		fmt.Fprint(w, "proxied")
	})
	handler := newApplicationHandler(
		metrics.Middleware(next),
		fixedCircuitState("closed"),
		cfg,
		metrics,
	)

	allowed := httptest.NewRequest(http.MethodOptions, "/v1/execute", nil)
	allowed.Header.Set("Origin", "http://localhost:3000")
	allowed.Header.Set("Access-Control-Request-Method", http.MethodPost)
	allowedResponse := httptest.NewRecorder()
	handler.ServeHTTP(allowedResponse, allowed)

	if allowedResponse.Code != http.StatusNoContent {
		t.Fatalf("allowed status = %d, want 204", allowedResponse.Code)
	}
	if got := allowedResponse.Header().Get("Access-Control-Expose-Headers"); got == "" {
		t.Fatal("allowed response does not expose gateway headers")
	}

	denied := httptest.NewRequest(http.MethodOptions, "/v1/execute", nil)
	denied.Header.Set("Origin", "https://untrusted.example")
	denied.Header.Set("Access-Control-Request-Method", http.MethodPost)
	deniedResponse := httptest.NewRecorder()
	handler.ServeHTTP(deniedResponse, denied)

	if deniedResponse.Code != http.StatusForbidden {
		t.Fatalf("denied status = %d, want 403", deniedResponse.Code)
	}
	if hits != 0 || metrics.requests.Load() != 0 {
		t.Fatalf("preflight reached proxy: hits=%d metrics=%d", hits, metrics.requests.Load())
	}
}

func TestObservabilityRejectsMutationAndHasNoCacheRateWithoutEligibleTraffic(t *testing.T) {
	cfg := DefaultConfig()
	metrics := newGatewayMetrics(false)
	handler := newApplicationHandler(
		metrics.Middleware(http.NotFoundHandler()),
		fixedCircuitState("closed"),
		cfg,
		metrics,
	)

	post := httptest.NewRequest(http.MethodPost, observabilityPath, nil)
	postResponse := httptest.NewRecorder()
	handler.ServeHTTP(postResponse, post)
	if postResponse.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST status = %d, want 405", postResponse.Code)
	}

	get := httptest.NewRequest(http.MethodGet, observabilityPath, nil)
	getResponse := httptest.NewRecorder()
	handler.ServeHTTP(getResponse, get)
	var snapshot ObservabilitySnapshot
	if err := json.NewDecoder(getResponse.Body).Decode(&snapshot); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if snapshot.SemanticCache.Enabled || snapshot.SemanticCache.HitRate != nil {
		t.Fatalf("unexpected cache snapshot: %#v", snapshot.SemanticCache)
	}
}

func TestGatewayMetricsAreSafeUnderConcurrentTraffic(t *testing.T) {
	metrics := newGatewayMetrics(true)
	const requests = 200
	var waitGroup sync.WaitGroup
	var expectedRateLimited uint64
	var expectedErrors uint64

	for index := 0; index < requests; index++ {
		statusCode := http.StatusOK
		if index%15 == 0 {
			statusCode = http.StatusTooManyRequests
			expectedRateLimited++
		} else if index%10 == 0 {
			statusCode = http.StatusBadGateway
			expectedErrors++
		}

		header := make(http.Header)
		if index%2 == 0 {
			header.Set(cache.HeaderCache, "hit")
			header.Set(coremeshRouteHeader, "primary")
		} else {
			header.Set(cache.HeaderCache, "miss")
			header.Set(coremeshRouteHeader, "fallback")
		}

		waitGroup.Add(1)
		go func(code int, responseHeader http.Header) {
			defer waitGroup.Done()
			metrics.record(code, responseHeader)
		}(statusCode, header)
	}
	waitGroup.Wait()

	snapshot := metrics.snapshot(DefaultConfig(), fixedCircuitState("closed"))
	if snapshot.Traffic.Requests != requests {
		t.Fatalf("requests = %d, want %d", snapshot.Traffic.Requests, requests)
	}
	if snapshot.Traffic.Primary != requests/2 ||
		snapshot.Traffic.Fallback != requests/2 {
		t.Fatalf("route counts = %#v, want %d each", snapshot.Traffic, requests/2)
	}
	if snapshot.Traffic.RateLimited != expectedRateLimited ||
		snapshot.Traffic.UpstreamErrors != expectedErrors {
		t.Fatalf(
			"outcomes = limited:%d errors:%d, want %d/%d",
			snapshot.Traffic.RateLimited,
			snapshot.Traffic.UpstreamErrors,
			expectedRateLimited,
			expectedErrors,
		)
	}
	if snapshot.SemanticCache.Hits != requests/2 ||
		snapshot.SemanticCache.Misses != requests/2 {
		t.Fatalf("cache counts = %#v, want %d each", snapshot.SemanticCache, requests/2)
	}
}

func TestAllowedOriginsCSVIsTrimmedDeduplicatedAndValidated(t *testing.T) {
	t.Setenv(
		"GATEWAY_ALLOWED_ORIGINS",
		" http://localhost:3000,https://ui.example,http://localhost:3000 ",
	)
	origins := envCSV("GATEWAY_ALLOWED_ORIGINS", nil)
	if len(origins) != 2 ||
		origins[0] != "http://localhost:3000" ||
		origins[1] != "https://ui.example" {
		t.Fatalf("origins = %#v, want trimmed unique entries", origins)
	}

	cfg := DefaultConfig()
	cfg.AllowedOrigins = origins
	if err := cfg.Validate(); err != nil {
		t.Fatalf("valid origins rejected: %v", err)
	}

	for _, invalid := range []string{
		"http://localhost:3000/path",
		"http://user@localhost:3000",
		"file://localhost",
	} {
		cfg.AllowedOrigins = []string{invalid}
		if err := cfg.Validate(); err == nil {
			t.Fatalf("invalid origin %q was accepted", invalid)
		}
	}
}
