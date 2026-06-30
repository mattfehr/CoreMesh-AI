package gateway

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestRedisTokenBucketEnforcesCapacity(t *testing.T) {
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })

	bucket := NewRedisTokenBucket(client, 5, 10)
	ctx := context.Background()

	for i := int64(1); i <= 5; i++ {
		result, err := bucket.Allow(ctx, "team-a")
		if err != nil {
			t.Fatalf("request %d: %v", i, err)
		}
		if !result.Allowed {
			t.Fatalf("request %d should be allowed", i)
		}
	}

	result, err := bucket.Allow(ctx, "team-a")
	if err != nil {
		t.Fatalf("6th request error: %v", err)
	}
	if result.Allowed {
		t.Fatal("6th request should be denied")
	}
	if result.Remaining != 0 {
		t.Fatalf("remaining = %d, want 0", result.Remaining)
	}
	if result.RetryAfter <= 0 {
		t.Fatalf("retry after = %v, want > 0", result.RetryAfter)
	}
}

func TestRedisTokenBucketConcurrentEnforcement(t *testing.T) {
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })

	const capacity int64 = 100
	bucket := NewRedisTokenBucket(client, capacity, 20)
	ctx := context.Background()

	var allowed atomic.Int64
	var denied atomic.Int64
	var wg sync.WaitGroup

	for i := 0; i < 200; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			result, err := bucket.Allow(ctx, "load-test")
			if err != nil {
				t.Errorf("allow: %v", err)
				return
			}
			if result.Allowed {
				allowed.Add(1)
				return
			}
			denied.Add(1)
		}()
	}
	wg.Wait()

	if got := allowed.Load(); got != capacity {
		t.Fatalf("allowed = %d, want %d", got, capacity)
	}
	if got := denied.Load(); got != 200-capacity {
		t.Fatalf("denied = %d, want %d", denied.Load(), 200-capacity)
	}
}

func TestRedisTokenBucketSetsRemainingHeaderThroughProxy(t *testing.T) {
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })

	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer primary.Close()

	fallback := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("fallback should not be called")
	}))
	defer fallback.Close()

	proxy, err := NewProxy(Config{
		PrimaryURL:               primary.URL,
		FallbackURL:              fallback.URL,
		RateLimitCapacity:        3,
		RateLimitRefillPerSecond: 1,
	}, NewRedisTokenBucket(client, 3, 1))
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	req.Header.Set("X-Team-ID", "team-redis")
	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
	if got := rec.Header().Get(rateLimitHeaderRemaining); got != "2" {
		t.Fatalf("%s = %q, want 2", rateLimitHeaderRemaining, got)
	}
}
