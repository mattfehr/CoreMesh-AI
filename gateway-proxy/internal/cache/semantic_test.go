package cache

import (
	"context"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

type fakeEmbedder struct {
	vectors map[string][]float64
}

func (e fakeEmbedder) Embed(_ context.Context, text string) ([]float64, error) {
	if vector, ok := e.vectors[text]; ok {
		return append([]float64(nil), vector...), nil
	}
	return []float64{1, 0, 0}, nil
}

type countingEmbedder struct {
	count atomic.Int64
}

func (e *countingEmbedder) Embed(context.Context, string) ([]float64, error) {
	e.count.Add(1)
	return []float64{1, 0, 0}, nil
}

type memoryStore struct {
	mu      sync.Mutex
	entries map[string]Entry
	hits    map[string]int64
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		entries: make(map[string]Entry),
		hits:    make(map[string]int64),
	}
}

func (s *memoryStore) EnsureIndex(context.Context, int) error {
	return nil
}

func (s *memoryStore) Lookup(_ context.Context, query LookupQuery) (LookupResult, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := query.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}

	var best Entry
	var bestKey string
	var bestSimilarity float64
	for key, entry := range s.entries {
		if entry.ScopeHash != query.ScopeHash || !entry.ExpiresAt.After(now) {
			continue
		}
		similarity := cosine(query.Vector, entry.Vector)
		if similarity >= query.Threshold && similarity > bestSimilarity {
			best = entry
			bestKey = key
			bestSimilarity = similarity
		}
	}
	if bestKey == "" {
		return LookupResult{}, false, nil
	}
	return LookupResult{
		Key:         bestKey,
		Similarity:  bestSimilarity,
		StatusCode:  best.StatusCode,
		ContentType: best.ContentType,
		Body:        append([]byte(nil), best.Body...),
	}, true, nil
}

func (s *memoryStore) Store(_ context.Context, entry Entry) error {
	if len(entry.Vector) == 0 {
		return errMissingVector
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entry.Vector = append([]float64(nil), entry.Vector...)
	entry.Body = append([]byte(nil), entry.Body...)
	s.entries[entry.Key] = entry
	return nil
}

func (s *memoryStore) IncrementHitCount(_ context.Context, key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hits[key]++
	return nil
}

func (s *memoryStore) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.entries)
}

func TestSemanticCacheReturnsRephrasedPromptFromCacheUnder15ms(t *testing.T) {
	store := newMemoryStore()
	semanticCache := newTestSemanticCache(t, store, fakeEmbedder{
		vectors: map[string][]float64{
			"Explain how Redis token buckets work.": {1, 0, 0},
			"How do Redis token buckets operate?":   {0.999, 0.02, 0},
		},
	})

	var backendHits atomic.Int64
	handler := semanticCache.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		backendHits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"answer":"redis token buckets refill over time"}`)
	}))

	first := httptest.NewRecorder()
	handler.ServeHTTP(first, jsonRequest(`{"model":"gpt-4o-mini","prompt":"Explain how Redis token buckets work.","temperature":0}`))
	if first.Code != http.StatusOK {
		t.Fatalf("first status = %d, want %d", first.Code, http.StatusOK)
	}
	if got := first.Header().Get(HeaderCache); got != cacheHeaderMiss {
		t.Fatalf("first %s = %q, want %q", HeaderCache, got, cacheHeaderMiss)
	}

	started := time.Now()
	second := httptest.NewRecorder()
	handler.ServeHTTP(second, jsonRequest(`{"model":"gpt-4o-mini","prompt":"How do Redis token buckets operate?","temperature":0}`))
	elapsed := time.Since(started)

	if second.Code != http.StatusOK {
		t.Fatalf("second status = %d, want %d", second.Code, http.StatusOK)
	}
	if got := second.Header().Get(HeaderCache); got != cacheHeaderHit {
		t.Fatalf("second %s = %q, want %q", HeaderCache, got, cacheHeaderHit)
	}
	if second.Body.String() != first.Body.String() {
		t.Fatalf("second body = %q, want cached %q", second.Body.String(), first.Body.String())
	}
	if backendHits.Load() != 1 {
		t.Fatalf("backend hits = %d, want 1", backendHits.Load())
	}
	if elapsed >= 15*time.Millisecond {
		t.Fatalf("cached response took %s, want <15ms", elapsed)
	}
}

func TestSemanticCacheBuffersStreamingResponseAfterCompletion(t *testing.T) {
	store := newMemoryStore()
	semanticCache := newTestSemanticCache(t, store, fakeEmbedder{
		vectors: map[string][]float64{
			"Explain circuit breaker fallback.":  {1, 0, 0},
			"Describe circuit breaker failover.": {0.998, 0.04, 0},
		},
	})

	var backendHits atomic.Int64
	streamBody := "data: first\n\ndata: second\n\n"
	handler := semanticCache.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		backendHits.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: first\n\n")
		if flusher, ok := w.(http.Flusher); ok {
			flusher.Flush()
		}
		if got := store.count(); got != 0 {
			t.Fatalf("cache stored before stream completion: %d entries", got)
		}
		fmt.Fprint(w, "data: second\n\n")
	}))

	first := httptest.NewRecorder()
	handler.ServeHTTP(first, jsonRequest(`{"model":"gpt-4o-mini","stream":true,"messages":[{"role":"system","content":"Be concise."},{"role":"user","content":"Explain circuit breaker fallback."}]}`))
	if first.Body.String() != streamBody {
		t.Fatalf("first stream body = %q, want %q", first.Body.String(), streamBody)
	}
	if got := first.Header().Get(HeaderCache); got != cacheHeaderMiss {
		t.Fatalf("first %s = %q, want %q", HeaderCache, got, cacheHeaderMiss)
	}
	if got := store.count(); got != 1 {
		t.Fatalf("stored entries = %d, want 1", got)
	}

	second := httptest.NewRecorder()
	handler.ServeHTTP(second, jsonRequest(`{"model":"gpt-4o-mini","stream":true,"messages":[{"role":"system","content":"Be concise."},{"role":"user","content":"Describe circuit breaker failover."}]}`))
	if second.Body.String() != streamBody {
		t.Fatalf("second stream body = %q, want cached %q", second.Body.String(), streamBody)
	}
	if got := second.Header().Get(HeaderCache); got != cacheHeaderHit {
		t.Fatalf("second %s = %q, want %q", HeaderCache, got, cacheHeaderHit)
	}
	if backendHits.Load() != 1 {
		t.Fatalf("backend hits = %d, want 1", backendHits.Load())
	}
}

func TestSemanticCacheBypassRestoresMalformedJSONBody(t *testing.T) {
	semanticCache := newTestSemanticCache(t, newMemoryStore(), fakeEmbedder{})
	handler := semanticCache.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("backend read body: %v", err)
		}
		fmt.Fprint(w, string(body))
	}))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, jsonRequest(`{"prompt":`))

	if got := rec.Header().Get(HeaderCache); got != cacheHeaderBypass {
		t.Fatalf("%s = %q, want %q", HeaderCache, got, cacheHeaderBypass)
	}
	if rec.Body.String() != `{"prompt":` {
		t.Fatalf("backend body = %q, want original malformed body", rec.Body.String())
	}
}

func TestSemanticCachePolicyBypassSkipsLookupAndStore(t *testing.T) {
	store := newMemoryStore()
	embedder := &countingEmbedder{}
	semanticCache := newTestSemanticCache(t, store, embedder)
	handler := semanticCache.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("backend read body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, string(body))
	}))

	rec := httptest.NewRecorder()
	req := jsonRequest(`{"model":"gpt-4o","prompt":"Analyze this complex code path."}`)
	req.Header.Set(HeaderCachePolicy, cacheHeaderBypass)
	handler.ServeHTTP(rec, req)

	if got := rec.Header().Get(HeaderCache); got != cacheHeaderBypass {
		t.Fatalf("%s = %q, want %q", HeaderCache, got, cacheHeaderBypass)
	}
	if embedder.count.Load() != 0 {
		t.Fatalf("embedder calls = %d, want 0", embedder.count.Load())
	}
	if got := store.count(); got != 0 {
		t.Fatalf("stored entries = %d, want 0", got)
	}
	if rec.Body.String() != `{"model":"gpt-4o","prompt":"Analyze this complex code path."}` {
		t.Fatalf("backend body = %q, want original body", rec.Body.String())
	}
}

func TestRedisStoreVectorSearchIntegration(t *testing.T) {
	redisURL := strings.TrimSpace(os.Getenv("REDIS_STACK_URL"))
	if redisURL == "" {
		t.Skip("set REDIS_STACK_URL to run Redis Stack vector integration test")
	}

	options, err := redis.ParseURL(redisURL)
	if err != nil {
		t.Fatalf("parse REDIS_STACK_URL: %v", err)
	}
	options.Protocol = 2
	client := redis.NewClient(options)
	t.Cleanup(func() { _ = client.Close() })

	ctx := context.Background()
	if err := client.Ping(ctx).Err(); err != nil {
		t.Fatalf("redis ping: %v", err)
	}

	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	cfg := DefaultConfig()
	cfg.IndexName = "coremesh_semantic_cache_test_" + suffix
	cfg.KeyPrefix = "coremesh:semantic:test:" + suffix + ":"
	cfg.VectorDim = 3
	cfg.TTL = time.Minute
	store := NewRedisStore(client, cfg)
	t.Cleanup(func() {
		_ = client.FTDropIndexWithArgs(context.Background(), cfg.IndexName, &redis.FTDropIndexOptions{DeleteDocs: true}).Err()
	})

	if err := store.EnsureIndex(ctx, cfg.VectorDim); err != nil {
		t.Fatalf("EnsureIndex: %v", err)
	}

	now := time.Now().UTC()
	entry := Entry{
		Key:         cfg.KeyPrefix + "entry",
		Vector:      []float64{1, 0, 0},
		Prompt:      "Explain semantic caching.",
		ScopeHash:   hashString("integration-scope"),
		Model:       "gpt-4o-mini",
		SystemHash:  hashString(""),
		ParamsHash:  hashString(""),
		StatusCode:  http.StatusOK,
		ContentType: "application/json",
		Body:        []byte(`{"answer":"cached"}`),
		CreatedAt:   now,
		ExpiresAt:   now.Add(time.Minute),
	}
	if err := store.Store(ctx, entry); err != nil {
		t.Fatalf("Store: %v", err)
	}

	var result LookupResult
	var found bool
	for deadline := time.Now().Add(time.Second); time.Now().Before(deadline); {
		result, found, err = store.Lookup(ctx, LookupQuery{
			Vector:    []float64{0.999, 0.001, 0},
			ScopeHash: entry.ScopeHash,
			Threshold: 0.96,
			Now:       now,
		})
		if err != nil {
			t.Fatalf("Lookup: %v", err)
		}
		if found {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if !found {
		t.Fatal("expected Redis vector lookup hit")
	}
	if result.Similarity < 0.96 {
		t.Fatalf("similarity = %.4f, want >= 0.96", result.Similarity)
	}
	if string(result.Body) != string(entry.Body) {
		t.Fatalf("body = %q, want %q", string(result.Body), string(entry.Body))
	}
}

func newTestSemanticCache(t *testing.T, store Store, embedder Embedder) *SemanticCache {
	t.Helper()
	semanticCache, err := NewSemanticCache(Config{
		Enabled:   true,
		Threshold: 0.96,
		TTL:       time.Minute,
		IndexName: "test_cache",
		KeyPrefix: "test:cache:",
		VectorDim: 3,
	}, store, embedder)
	if err != nil {
		t.Fatalf("NewSemanticCache: %v", err)
	}
	return semanticCache
}

func jsonRequest(body string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req
}

func cosine(left []float64, right []float64) float64 {
	if len(left) != len(right) || len(left) == 0 {
		return 0
	}
	var dot float64
	var leftNorm float64
	var rightNorm float64
	for i := range left {
		dot += left[i] * right[i]
		leftNorm += left[i] * left[i]
		rightNorm += right[i] * right[i]
	}
	if leftNorm == 0 || rightNorm == 0 {
		return 0
	}
	return dot / (math.Sqrt(leftNorm) * math.Sqrt(rightNorm))
}
