// Module: semantic-cache provider configuration and local embedding tests.
// System role: protects credential-free cache validation and provider selection.
// Dependencies: Go testing and process-local cache fakes from semantic_test.go.
// Side effects: temporarily changes process environment variables per test.
package cache

import (
	"context"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestConfigFromEnvAcceptsHashProviderWithoutOpenAIKey(t *testing.T) {
	clearSemanticCacheEnv(t)
	t.Setenv("SEMANTIC_CACHE_ENABLED", "true")
	t.Setenv("SEMANTIC_CACHE_EMBEDDING_PROVIDER", "HASH")

	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatalf("ConfigFromEnv: %v", err)
	}
	if !cfg.Enabled {
		t.Fatal("semantic cache should be enabled")
	}
	if cfg.EmbeddingProvider != EmbeddingProviderHash {
		t.Fatalf("embedding provider = %q, want %q", cfg.EmbeddingProvider, EmbeddingProviderHash)
	}
	if cfg.OpenAIAPIKey != "" {
		t.Fatalf("OpenAI API key = %q, want empty", cfg.OpenAIAPIKey)
	}
}

func TestConfigFromEnvRequiresOpenAIKeyOnlyWhenEnabledWithOpenAI(t *testing.T) {
	t.Run("enabled openai", func(t *testing.T) {
		clearSemanticCacheEnv(t)
		t.Setenv("SEMANTIC_CACHE_ENABLED", "true")
		t.Setenv("SEMANTIC_CACHE_EMBEDDING_PROVIDER", EmbeddingProviderOpenAI)

		_, err := ConfigFromEnv()
		if err == nil || !strings.Contains(err.Error(), "OPENAI_API_KEY") {
			t.Fatalf("ConfigFromEnv error = %v, want missing OPENAI_API_KEY", err)
		}
	})

	t.Run("disabled openai", func(t *testing.T) {
		clearSemanticCacheEnv(t)
		t.Setenv("SEMANTIC_CACHE_ENABLED", "false")
		t.Setenv("SEMANTIC_CACHE_EMBEDDING_PROVIDER", EmbeddingProviderOpenAI)

		cfg, err := ConfigFromEnv()
		if err != nil {
			t.Fatalf("ConfigFromEnv: %v", err)
		}
		if cfg.Enabled {
			t.Fatal("semantic cache should remain disabled")
		}
	})
}

func TestConfigFromEnvRejectsUnknownEmbeddingProvider(t *testing.T) {
	clearSemanticCacheEnv(t)
	t.Setenv("SEMANTIC_CACHE_EMBEDDING_PROVIDER", "local-magic")

	_, err := ConfigFromEnv()
	if err == nil || !strings.Contains(err.Error(), "SEMANTIC_CACHE_EMBEDDING_PROVIDER") {
		t.Fatalf("ConfigFromEnv error = %v, want provider validation error", err)
	}
}

func TestConfigFromEnvLoadsStorageNamespaceAndVectorDimension(t *testing.T) {
	clearSemanticCacheEnv(t)
	t.Setenv("SEMANTIC_CACHE_KEY_PREFIX", "test:semantic:")
	t.Setenv("SEMANTIC_CACHE_VECTOR_DIM", "64")

	cfg, err := ConfigFromEnv()
	if err != nil {
		t.Fatalf("ConfigFromEnv: %v", err)
	}
	if cfg.KeyPrefix != "test:semantic:" {
		t.Fatalf("key prefix = %q, want %q", cfg.KeyPrefix, "test:semantic:")
	}
	if cfg.VectorDim != 64 {
		t.Fatalf("vector dimension = %d, want 64", cfg.VectorDim)
	}
}

func TestHashEmbedderIsDeterministicAndNormalized(t *testing.T) {
	embedder, err := NewHashEmbedder(Config{VectorDim: 64, EmbeddingProvider: EmbeddingProviderHash})
	if err != nil {
		t.Fatalf("NewHashEmbedder: %v", err)
	}

	first, err := embedder.Embed(context.Background(), "Redis cache cache 42!")
	if err != nil {
		t.Fatalf("first Embed: %v", err)
	}
	second, err := embedder.Embed(context.Background(), "Redis cache cache 42!")
	if err != nil {
		t.Fatalf("second Embed: %v", err)
	}
	if len(first) != 64 || len(second) != 64 {
		t.Fatalf("embedding lengths = %d and %d, want 64", len(first), len(second))
	}
	for i := range first {
		if first[i] != second[i] {
			t.Fatalf("embedding differs at index %d: %v != %v", i, first[i], second[i])
		}
	}
	if norm := vectorNorm(first); math.Abs(norm-1) > 1e-12 {
		t.Fatalf("embedding norm = %.16f, want 1", norm)
	}

	punctuation, err := embedder.Embed(context.Background(), "!!!")
	if err != nil {
		t.Fatalf("punctuation Embed: %v", err)
	}
	if norm := vectorNorm(punctuation); math.Abs(norm-1) > 1e-12 {
		t.Fatalf("punctuation embedding norm = %.16f, want 1", norm)
	}
}

func TestHashProviderProducesByteIdenticalRepeatHit(t *testing.T) {
	store := newMemoryStore()
	embedder, err := NewEmbedder(Config{VectorDim: 64, EmbeddingProvider: EmbeddingProviderHash})
	if err != nil {
		t.Fatalf("NewEmbedder: %v", err)
	}
	semanticCache, err := NewSemanticCache(Config{
		Enabled:           true,
		Threshold:         0.96,
		TTL:               time.Minute,
		IndexName:         "test_hash_cache",
		KeyPrefix:         "test:hash-cache:",
		VectorDim:         64,
		EmbeddingProvider: EmbeddingProviderHash,
	}, store, embedder)
	if err != nil {
		t.Fatalf("NewSemanticCache: %v", err)
	}

	var backendHits atomic.Int64
	handler := semanticCache.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		backendHits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"answer\":\"deterministic bytes\"}\n")
	}))
	requestBody := `{"model":"stub","messages":[{"role":"user","content":"unique hash cache request"}]}`

	first := httptest.NewRecorder()
	handler.ServeHTTP(first, jsonRequest(requestBody))
	second := httptest.NewRecorder()
	handler.ServeHTTP(second, jsonRequest(requestBody))

	if got := first.Header().Get(HeaderCache); got != cacheHeaderMiss {
		t.Fatalf("first %s = %q, want %q", HeaderCache, got, cacheHeaderMiss)
	}
	if got := second.Header().Get(HeaderCache); got != cacheHeaderHit {
		t.Fatalf("second %s = %q, want %q", HeaderCache, got, cacheHeaderHit)
	}
	if first.Body.String() != second.Body.String() {
		t.Fatalf("cached body = %q, want byte-identical %q", second.Body.String(), first.Body.String())
	}
	if backendHits.Load() != 1 {
		t.Fatalf("backend hits = %d, want 1", backendHits.Load())
	}
}

func clearSemanticCacheEnv(t *testing.T) {
	t.Helper()
	for _, name := range []string{
		"SEMANTIC_CACHE_ENABLED",
		"SEMANTIC_CACHE_EMBEDDING_PROVIDER",
		"SEMANTIC_CACHE_INDEX",
		"SEMANTIC_CACHE_KEY_PREFIX",
		"SEMANTIC_CACHE_THRESHOLD",
		"SEMANTIC_CACHE_TTL",
		"SEMANTIC_CACHE_VECTOR_DIM",
		"OPENAI_API_KEY",
		"OPENAI_BASE_URL",
		"OPENAI_EMBEDDING_MODEL",
	} {
		t.Setenv(name, "")
	}
}
