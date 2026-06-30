package gateway

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type staticLimiter struct {
	result LimitResult
	err    error
	keys   []string
	mu     sync.Mutex
}

func (l *staticLimiter) Allow(_ context.Context, key string) (LimitResult, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.keys = append(l.keys, key)
	return l.result, l.err
}

type countingLimiter struct {
	capacity int64
	count    atomic.Int64
}

func (l *countingLimiter) Allow(_ context.Context, _ string) (LimitResult, error) {
	count := l.count.Add(1)
	if count <= l.capacity {
		return LimitResult{Allowed: true, Remaining: l.capacity - count}, nil
	}
	return LimitResult{Allowed: false, Remaining: 0, RetryAfter: time.Second}, nil
}

func TestRateLimitDeniedSetsHeadersAndSkipsBackend(t *testing.T) {
	var primaryHits atomic.Int64
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		primaryHits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("fallback should not be called when rate limited")
	}))
	defer fallback.Close()

	limiter := &staticLimiter{
		result: LimitResult{Allowed: false, Remaining: 0, RetryAfter: 2 * time.Second},
	}
	proxy := newTestProxy(t, primary.URL, fallback.URL, limiter)

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	req.Header.Set("X-Team-ID", "finance")
	rec := httptest.NewRecorder()

	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusTooManyRequests)
	}
	if got := rec.Header().Get(rateLimitHeaderRemaining); got != "0" {
		t.Fatalf("%s = %q, want 0", rateLimitHeaderRemaining, got)
	}
	if got := rec.Header().Get("Retry-After"); got != "2" {
		t.Fatalf("Retry-After = %q, want 2", got)
	}
	if primaryHits.Load() != 0 {
		t.Fatalf("primary hits = %d, want 0", primaryHits.Load())
	}
	if got := limiter.keys; len(got) != 1 || got[0] != "finance" {
		t.Fatalf("limiter keys = %#v, want [finance]", got)
	}
}

func TestAllowedRequestIncludesRemainingHeader(t *testing.T) {
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "primary")
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("fallback should not be called")
	}))
	defer fallback.Close()

	proxy := newTestProxy(t, primary.URL, fallback.URL, &staticLimiter{
		result: LimitResult{Allowed: true, Remaining: 99},
	})

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
	if got := rec.Header().Get(rateLimitHeaderRemaining); got != "99" {
		t.Fatalf("%s = %q, want 99", rateLimitHeaderRemaining, got)
	}
	if got := rec.Header().Get(coremeshRouteHeader); got != "primary" {
		t.Fatalf("%s = %q, want primary", coremeshRouteHeader, got)
	}
}

func TestConcurrentLimitEnforcementDropsExcessTraffic(t *testing.T) {
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("fallback should not be called")
	}))
	defer fallback.Close()

	proxy := newTestProxy(t, primary.URL, fallback.URL, &countingLimiter{capacity: 100})
	server := httptest.NewServer(proxy)
	defer server.Close()

	var okCount atomic.Int64
	var tooManyCount atomic.Int64
	var wg sync.WaitGroup

	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			req, err := http.NewRequest(http.MethodGet, server.URL+"/health", nil)
			if err != nil {
				t.Errorf("new request: %v", err)
				return
			}
			req.Header.Set("X-Team-ID", "load-test")

			resp, err := server.Client().Do(req)
			if err != nil {
				t.Errorf("request failed: %v", err)
				return
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()

			switch resp.StatusCode {
			case http.StatusOK:
				okCount.Add(1)
			case http.StatusTooManyRequests:
				tooManyCount.Add(1)
			default:
				t.Errorf("unexpected status %d", resp.StatusCode)
			}
		}()
	}
	wg.Wait()

	if okCount.Load() != 100 {
		t.Fatalf("ok count = %d, want 100", okCount.Load())
	}
	if tooManyCount.Load() != 100 {
		t.Fatalf("429 count = %d, want 100", tooManyCount.Load())
	}
}

func TestCircuitOpensAfterPrimaryFailuresAndRoutesToFallback(t *testing.T) {
	var primaryHits atomic.Int64
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		primaryHits.Add(1)
		http.Error(w, "primary failed", http.StatusBadGateway)
	}))
	defer primary.Close()

	var fallbackHits atomic.Int64
	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fallbackHits.Add(1)
		fmt.Fprint(w, "fallback")
	}))
	defer fallback.Close()

	proxy := newTestProxy(t, primary.URL, fallback.URL, &staticLimiter{
		result: LimitResult{Allowed: true, Remaining: 99},
	})

	for i := 0; i < 5; i++ {
		rec := httptest.NewRecorder()
		proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
		if i < 4 {
			if rec.Code != http.StatusBadGateway {
				t.Fatalf("request %d status = %d, want %d", i+1, rec.Code, http.StatusBadGateway)
			}
			continue
		}
		if rec.Code != http.StatusOK {
			t.Fatalf("request %d status = %d, want %d", i+1, rec.Code, http.StatusOK)
		}
	}

	if got := proxy.CircuitState(); got != string(circuitOpen) {
		t.Fatalf("circuit state = %q, want open", got)
	}

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("open-circuit request status = %d, want %d", rec.Code, http.StatusOK)
	}
	if body := rec.Body.String(); body != "fallback" {
		t.Fatalf("body = %q, want fallback", body)
	}
	if primaryHits.Load() != 5 {
		t.Fatalf("primary hits = %d, want 5", primaryHits.Load())
	}
	if fallbackHits.Load() != 2 {
		t.Fatalf("fallback hits = %d, want 2", fallbackHits.Load())
	}
}

func TestClosedCircuitSingleFailureDoesNotUseFallback(t *testing.T) {
	var primaryHits atomic.Int64
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		primaryHits.Add(1)
		http.Error(w, "primary failed", http.StatusBadGateway)
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("fallback should not be called while circuit is closed")
	}))
	defer fallback.Close()

	proxy := newTestProxy(t, primary.URL, fallback.URL, &staticLimiter{
		result: LimitResult{Allowed: true, Remaining: 99},
	})

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadGateway)
	}
	if got := proxy.CircuitState(); got != string(circuitClosed) {
		t.Fatalf("circuit state = %q, want closed", got)
	}
	if primaryHits.Load() != 1 {
		t.Fatalf("primary hits = %d, want 1", primaryHits.Load())
	}
}

func TestHalfOpenProbeTimeoutClearsInFlightFlag(t *testing.T) {
	breaker := NewCircuitBreaker(5, 30*time.Second, 30*time.Second)
	breaker.halfOpenProbeTimeout = 10 * time.Millisecond

	now := time.Now()
	breaker.mu.Lock()
	breaker.state = circuitHalfOpen
	breaker.halfOpenInFlight = true
	breaker.halfOpenStartedAt = now.Add(-20 * time.Millisecond)
	breaker.mu.Unlock()

	route := breaker.Route(now)
	if route.UseFallback {
		t.Fatal("stale half-open probe should allow a new primary attempt")
	}
	if got := breaker.State(); got != string(circuitHalfOpen) {
		t.Fatalf("circuit state = %q, want half-open", got)
	}
}

func TestCircuitHalfOpenClosesAfterSuccessfulProbe(t *testing.T) {
	var primaryHits atomic.Int64
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hit := primaryHits.Add(1)
		if hit <= 5 {
			http.Error(w, "primary failed", http.StatusInternalServerError)
			return
		}
		fmt.Fprint(w, "primary-ok")
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "fallback")
	}))
	defer fallback.Close()

	proxy := newTestProxy(t, primary.URL, fallback.URL, &staticLimiter{
		result: LimitResult{Allowed: true, Remaining: 99},
	})
	proxy.breaker.openDuration = 10 * time.Millisecond

	for i := 0; i < 5; i++ {
		rec := httptest.NewRecorder()
		proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
	}
	if got := proxy.CircuitState(); got != string(circuitOpen) {
		t.Fatalf("circuit state = %q, want open", got)
	}

	time.Sleep(20 * time.Millisecond)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
	if body := rec.Body.String(); body != "primary-ok" {
		t.Fatalf("half-open body = %q, want primary-ok", body)
	}
	if got := proxy.CircuitState(); got != string(circuitClosed) {
		t.Fatalf("circuit state = %q, want closed", got)
	}

	rec = httptest.NewRecorder()
	proxy.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat", nil))
	if body := rec.Body.String(); body != "primary-ok" {
		t.Fatalf("closed-circuit body = %q, want primary-ok", body)
	}
}

func newTestProxy(t *testing.T, primaryURL string, fallbackURL string, limiter RateLimiter) *Proxy {
	t.Helper()

	proxy, err := NewProxy(Config{
		PrimaryURL:               primaryURL,
		FallbackURL:              fallbackURL,
		RateLimitCapacity:        100,
		RateLimitRefillPerSecond: 20,
		CircuitFailureThreshold:  5,
		CircuitFailureWindow:     30 * time.Second,
		CircuitOpenDuration:      30 * time.Second,
	}, limiter)
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	return proxy
}
