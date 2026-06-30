// Package cache contains the CoreMesh semantic response cache.
package cache

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	defaultThreshold      = 0.96
	defaultTTL            = 24 * time.Hour
	defaultIndexName      = "coremesh_semantic_cache"
	defaultKeyPrefix      = "coremesh:semantic:entry:"
	defaultVectorDim      = 1536
	defaultEmbeddingModel = "text-embedding-3-small"
	defaultOpenAIBaseURL  = "https://api.openai.com/v1"

	// HeaderCache reports whether semantic cache handling was a hit, miss, or bypass.
	HeaderCache = "x-coremesh-cache"

	cacheHeaderHit    = "hit"
	cacheHeaderMiss   = "miss"
	cacheHeaderBypass = "bypass"
)

// Config controls semantic cache behavior.
type Config struct {
	Enabled        bool
	Threshold      float64
	TTL            time.Duration
	IndexName      string
	KeyPrefix      string
	VectorDim      int
	OpenAIAPIKey   string
	OpenAIBaseURL  string
	EmbeddingModel string
}

// DefaultConfig returns production-oriented semantic cache defaults.
func DefaultConfig() Config {
	return Config{
		Threshold:      defaultThreshold,
		TTL:            defaultTTL,
		IndexName:      defaultIndexName,
		KeyPrefix:      defaultKeyPrefix,
		VectorDim:      defaultVectorDim,
		OpenAIBaseURL:  defaultOpenAIBaseURL,
		EmbeddingModel: defaultEmbeddingModel,
	}
}

// ConfigFromEnv loads semantic cache settings from environment variables.
func ConfigFromEnv() (Config, error) {
	cfg := DefaultConfig()
	cfg.OpenAIAPIKey = strings.TrimSpace(os.Getenv("OPENAI_API_KEY"))
	cfg.OpenAIBaseURL = envString("OPENAI_BASE_URL", cfg.OpenAIBaseURL)
	cfg.EmbeddingModel = envString("OPENAI_EMBEDDING_MODEL", cfg.EmbeddingModel)
	cfg.IndexName = envString("SEMANTIC_CACHE_INDEX", cfg.IndexName)

	enabledRaw := strings.TrimSpace(os.Getenv("SEMANTIC_CACHE_ENABLED"))
	if enabledRaw == "" {
		cfg.Enabled = cfg.OpenAIAPIKey != ""
	} else {
		enabled, err := strconv.ParseBool(enabledRaw)
		if err != nil {
			return Config{}, fmt.Errorf("invalid SEMANTIC_CACHE_ENABLED: %w", err)
		}
		cfg.Enabled = enabled
	}

	var err error
	if cfg.Threshold, err = envFloat64("SEMANTIC_CACHE_THRESHOLD", cfg.Threshold); err != nil {
		return Config{}, err
	}
	if cfg.TTL, err = envDuration("SEMANTIC_CACHE_TTL", cfg.TTL); err != nil {
		return Config{}, err
	}

	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate checks the semantic cache configuration.
func (c Config) Validate() error {
	if c.Threshold <= 0 || c.Threshold > 1 {
		return fmt.Errorf("SEMANTIC_CACHE_THRESHOLD must be in (0, 1]")
	}
	if c.TTL <= 0 {
		return fmt.Errorf("SEMANTIC_CACHE_TTL must be greater than zero")
	}
	if strings.TrimSpace(c.IndexName) == "" {
		return fmt.Errorf("SEMANTIC_CACHE_INDEX is required")
	}
	if strings.TrimSpace(c.KeyPrefix) == "" {
		return fmt.Errorf("semantic cache key prefix is required")
	}
	if c.VectorDim <= 0 {
		return fmt.Errorf("semantic cache vector dimension must be greater than zero")
	}
	if strings.TrimSpace(c.OpenAIBaseURL) == "" {
		return fmt.Errorf("OPENAI_BASE_URL is required")
	}
	if strings.TrimSpace(c.EmbeddingModel) == "" {
		return fmt.Errorf("OPENAI_EMBEDDING_MODEL is required")
	}
	return nil
}

func (c Config) withDefaults() Config {
	defaults := DefaultConfig()
	if c.Threshold == 0 {
		c.Threshold = defaults.Threshold
	}
	if c.TTL == 0 {
		c.TTL = defaults.TTL
	}
	if c.IndexName == "" {
		c.IndexName = defaults.IndexName
	}
	if c.KeyPrefix == "" {
		c.KeyPrefix = defaults.KeyPrefix
	}
	if c.VectorDim == 0 {
		c.VectorDim = defaults.VectorDim
	}
	if c.OpenAIBaseURL == "" {
		c.OpenAIBaseURL = defaults.OpenAIBaseURL
	}
	if c.EmbeddingModel == "" {
		c.EmbeddingModel = defaults.EmbeddingModel
	}
	return c
}

// Embedder generates prompt embeddings.
type Embedder interface {
	Embed(ctx context.Context, text string) ([]float64, error)
}

// Store persists and searches semantic cache entries.
type Store interface {
	EnsureIndex(ctx context.Context, dim int) error
	Lookup(ctx context.Context, query LookupQuery) (LookupResult, bool, error)
	Store(ctx context.Context, entry Entry) error
	IncrementHitCount(ctx context.Context, key string) error
}

// LookupQuery is a nearest-neighbor cache lookup scoped to compatible requests.
type LookupQuery struct {
	Vector    []float64
	ScopeHash string
	Threshold float64
	Now       time.Time
}

// LookupResult is a cache hit returned by Store.Lookup.
type LookupResult struct {
	Key         string
	Similarity  float64
	StatusCode  int
	ContentType string
	Body        []byte
}

// Entry is a complete successful backend response ready for cache storage.
type Entry struct {
	Key         string
	Vector      []float64
	Prompt      string
	ScopeHash   string
	Model       string
	SystemHash  string
	ParamsHash  string
	Stream      bool
	StatusCode  int
	ContentType string
	Body        []byte
	CreatedAt   time.Time
	ExpiresAt   time.Time
}

// SemanticCache is HTTP middleware that serves semantic response hits and
// buffers successful misses back into Redis.
type SemanticCache struct {
	cfg      Config
	store    Store
	embedder Embedder

	ensureMu   sync.Mutex
	ensureDone bool
	ensureErr  error
}

// NewSemanticCache builds semantic-cache middleware with injected storage and embedding.
func NewSemanticCache(cfg Config, store Store, embedder Embedder) (*SemanticCache, error) {
	cfg = cfg.withDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if store == nil {
		return nil, fmt.Errorf("semantic cache store is required")
	}
	if embedder == nil {
		return nil, fmt.Errorf("semantic cache embedder is required")
	}
	return &SemanticCache{
		cfg:      cfg,
		store:    store,
		embedder: embedder,
	}, nil
}

// Middleware wraps next with semantic cache lookup and response capture.
func (c *SemanticCache) Middleware(next http.Handler) http.Handler {
	if c == nil || !c.cfg.Enabled {
		return next
	}

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cacheReq, body, ok := extractRequest(r)
		if !ok {
			if body != nil {
				restoreRequestBody(r, body)
			}
			w.Header().Set(HeaderCache, cacheHeaderBypass)
			next.ServeHTTP(w, r)
			return
		}
		restoreRequestBody(r, body)

		vector, err := c.embedder.Embed(r.Context(), cacheReq.prompt)
		if err != nil {
			w.Header().Set(HeaderCache, cacheHeaderBypass)
			next.ServeHTTP(w, r)
			return
		}

		if err := c.ensureIndex(r.Context()); err != nil {
			w.Header().Set(HeaderCache, cacheHeaderBypass)
			next.ServeHTTP(w, r)
			return
		}

		hit, found, err := c.store.Lookup(r.Context(), LookupQuery{
			Vector:    vector,
			ScopeHash: cacheReq.scopeHash,
			Threshold: c.cfg.Threshold,
			Now:       time.Now().UTC(),
		})
		if err != nil {
			w.Header().Set(HeaderCache, cacheHeaderBypass)
			next.ServeHTTP(w, r)
			return
		}
		if found {
			if hit.ContentType != "" {
				w.Header().Set("Content-Type", hit.ContentType)
			}
			w.Header().Set(HeaderCache, cacheHeaderHit)
			w.Header().Set("Content-Length", strconv.Itoa(len(hit.Body)))
			statusCode := hit.StatusCode
			if statusCode == 0 {
				statusCode = http.StatusOK
			}
			w.WriteHeader(statusCode)
			_, _ = w.Write(hit.Body)
			_ = c.store.IncrementHitCount(r.Context(), hit.Key)
			return
		}

		w.Header().Set(HeaderCache, cacheHeaderMiss)
		capture := newCaptureResponseWriter(w)
		next.ServeHTTP(capture, r)

		statusCode := capture.statusCode()
		if capture.writeErr != nil || statusCode < http.StatusOK || statusCode >= http.StatusMultipleChoices {
			return
		}

		now := time.Now().UTC()
		contentType := capture.Header().Get("Content-Type")
		entry := Entry{
			Key:         c.entryKey(cacheReq.scopeHash, cacheReq.prompt, now),
			Vector:      vector,
			Prompt:      cacheReq.prompt,
			ScopeHash:   cacheReq.scopeHash,
			Model:       cacheReq.model,
			SystemHash:  cacheReq.systemHash,
			ParamsHash:  cacheReq.paramsHash,
			Stream:      cacheReq.stream,
			StatusCode:  statusCode,
			ContentType: contentType,
			Body:        capture.bodyBytes(),
			CreatedAt:   now,
			ExpiresAt:   now.Add(c.cfg.TTL),
		}
		_ = c.store.Store(r.Context(), entry)
	})
}

func (c *SemanticCache) ensureIndex(ctx context.Context) error {
	c.ensureMu.Lock()
	defer c.ensureMu.Unlock()
	if c.ensureDone {
		return c.ensureErr
	}
	c.ensureErr = c.store.EnsureIndex(ctx, c.cfg.VectorDim)
	c.ensureDone = c.ensureErr == nil
	return c.ensureErr
}

func (c *SemanticCache) entryKey(scopeHash string, prompt string, now time.Time) string {
	sum := sha256.Sum256([]byte(scopeHash + "\x00" + prompt + "\x00" + now.Format(time.RFC3339Nano)))
	return c.cfg.KeyPrefix + fmt.Sprintf("%x", sum[:])
}

// RedisStore stores semantic cache entries in Redis Stack hashes and searches
// them through a RediSearch HNSW vector index.
type RedisStore struct {
	client    *redis.Client
	indexName string
	keyPrefix string
	ttl       time.Duration
}

// NewRedisStore creates a Redis-backed semantic cache store.
func NewRedisStore(client *redis.Client, cfg Config) *RedisStore {
	cfg = cfg.withDefaults()
	return &RedisStore{
		client:    client,
		indexName: cfg.IndexName,
		keyPrefix: cfg.KeyPrefix,
		ttl:       cfg.TTL,
	}
}

// EnsureIndex creates the RediSearch vector index if it does not exist.
func (s *RedisStore) EnsureIndex(ctx context.Context, dim int) error {
	if s == nil || s.client == nil {
		return fmt.Errorf("redis client is required")
	}
	if _, err := s.client.FTInfo(ctx, s.indexName).Result(); err == nil {
		return nil
	} else if !isUnknownIndexError(err) {
		return err
	}

	_, err := s.client.FTCreate(
		ctx,
		s.indexName,
		&redis.FTCreateOptions{
			OnHash: true,
			Prefix: []interface{}{
				s.keyPrefix,
			},
		},
		&redis.FieldSchema{FieldName: "scope_hash", FieldType: redis.SearchFieldTypeTag},
		&redis.FieldSchema{FieldName: "model", FieldType: redis.SearchFieldTypeTag},
		&redis.FieldSchema{FieldName: "system_hash", FieldType: redis.SearchFieldTypeTag},
		&redis.FieldSchema{FieldName: "params_hash", FieldType: redis.SearchFieldTypeTag},
		&redis.FieldSchema{FieldName: "stream", FieldType: redis.SearchFieldTypeTag},
		&redis.FieldSchema{FieldName: "status", FieldType: redis.SearchFieldTypeNumeric},
		&redis.FieldSchema{FieldName: "created_at", FieldType: redis.SearchFieldTypeNumeric},
		&redis.FieldSchema{FieldName: "expires_at", FieldType: redis.SearchFieldTypeNumeric},
		&redis.FieldSchema{FieldName: "hit_count", FieldType: redis.SearchFieldTypeNumeric},
		&redis.FieldSchema{FieldName: "content_type", FieldType: redis.SearchFieldTypeText, NoIndex: true},
		&redis.FieldSchema{FieldName: "prompt", FieldType: redis.SearchFieldTypeText, NoIndex: true},
		&redis.FieldSchema{FieldName: "response_body", FieldType: redis.SearchFieldTypeText, NoIndex: true},
		&redis.FieldSchema{
			FieldName: "embedding",
			FieldType: redis.SearchFieldTypeVector,
			VectorArgs: &redis.FTVectorArgs{
				HNSWOptions: &redis.FTHNSWOptions{
					Type:                   "FLOAT32",
					Dim:                    dim,
					DistanceMetric:         "COSINE",
					MaxEdgesPerNode:        16,
					MaxAllowedEdgesPerNode: 200,
					EFRunTime:              10,
				},
			},
		},
	).Result()
	if err != nil && !isIndexExistsError(err) {
		return err
	}
	return nil
}

// Lookup returns the nearest semantically compatible cache entry above threshold.
func (s *RedisStore) Lookup(ctx context.Context, query LookupQuery) (LookupResult, bool, error) {
	now := query.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}
	redisQuery := fmt.Sprintf("(@scope_hash:{%s} @expires_at:[%d +inf])=>[KNN 1 @embedding $vector AS distance]", query.ScopeHash, now.Unix())
	result, err := s.client.FTSearchWithArgs(
		ctx,
		s.indexName,
		redisQuery,
		&redis.FTSearchOptions{
			Return: []redis.FTSearchReturn{
				{FieldName: "distance"},
				{FieldName: "status"},
				{FieldName: "content_type"},
				{FieldName: "response_body"},
			},
			SortBy: []redis.FTSearchSortBy{
				{FieldName: "distance", Asc: true},
			},
			LimitOffset:    0,
			Limit:          1,
			Params:         map[string]interface{}{"vector": vectorBlob(query.Vector)},
			DialectVersion: 2,
		},
	).Result()
	if err != nil {
		if isUnknownIndexError(err) {
			return LookupResult{}, false, nil
		}
		return LookupResult{}, false, err
	}
	if result.Total == 0 || len(result.Docs) == 0 {
		return LookupResult{}, false, nil
	}

	doc := result.Docs[0]
	distance, err := strconv.ParseFloat(doc.Fields["distance"], 64)
	if err != nil {
		return LookupResult{}, false, fmt.Errorf("parse redis vector distance: %w", err)
	}
	similarity := 1 - distance
	if similarity < query.Threshold {
		return LookupResult{}, false, nil
	}

	statusCode := http.StatusOK
	if rawStatus := strings.TrimSpace(doc.Fields["status"]); rawStatus != "" {
		if parsed, err := strconv.Atoi(rawStatus); err == nil {
			statusCode = parsed
		}
	}

	return LookupResult{
		Key:         doc.ID,
		Similarity:  similarity,
		StatusCode:  statusCode,
		ContentType: doc.Fields["content_type"],
		Body:        []byte(doc.Fields["response_body"]),
	}, true, nil
}

// Store writes a complete successful backend response into Redis.
func (s *RedisStore) Store(ctx context.Context, entry Entry) error {
	ttl := s.ttl
	if ttl <= 0 {
		ttl = time.Until(entry.ExpiresAt)
	}
	if ttl <= 0 {
		return nil
	}

	createdAt := entry.CreatedAt
	if createdAt.IsZero() {
		createdAt = time.Now().UTC()
	}
	expiresAt := entry.ExpiresAt
	if expiresAt.IsZero() {
		expiresAt = createdAt.Add(ttl)
	}

	pipe := s.client.TxPipeline()
	pipe.HSet(ctx, entry.Key, map[string]interface{}{
		"embedding":     vectorBlob(entry.Vector),
		"prompt":        entry.Prompt,
		"scope_hash":    entry.ScopeHash,
		"model":         entry.Model,
		"system_hash":   entry.SystemHash,
		"params_hash":   entry.ParamsHash,
		"stream":        strconv.FormatBool(entry.Stream),
		"status":        entry.StatusCode,
		"content_type":  entry.ContentType,
		"response_body": string(entry.Body),
		"created_at":    createdAt.Unix(),
		"expires_at":    expiresAt.Unix(),
		"hit_count":     0,
	})
	pipe.Expire(ctx, entry.Key, ttl)
	_, err := pipe.Exec(ctx)
	return err
}

// IncrementHitCount records one served cache hit.
func (s *RedisStore) IncrementHitCount(ctx context.Context, key string) error {
	return s.client.HIncrBy(ctx, key, "hit_count", 1).Err()
}

// OpenAIEmbedder calls the OpenAI embeddings API through net/http.
type OpenAIEmbedder struct {
	apiKey  string
	baseURL string
	model   string
	client  *http.Client
}

// NewOpenAIEmbedder creates an OpenAI embeddings client.
func NewOpenAIEmbedder(cfg Config) (*OpenAIEmbedder, error) {
	cfg = cfg.withDefaults()
	if strings.TrimSpace(cfg.OpenAIAPIKey) == "" {
		return nil, fmt.Errorf("OPENAI_API_KEY is required when semantic cache is enabled")
	}
	return &OpenAIEmbedder{
		apiKey:  strings.TrimSpace(cfg.OpenAIAPIKey),
		baseURL: strings.TrimRight(cfg.OpenAIBaseURL, "/"),
		model:   cfg.EmbeddingModel,
		client:  http.DefaultClient,
	}, nil
}

// Embed returns a single embedding vector for text.
func (e *OpenAIEmbedder) Embed(ctx context.Context, text string) ([]float64, error) {
	payload, err := json.Marshal(map[string]interface{}{
		"model": e.model,
		"input": text,
	})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.baseURL+"/embeddings", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+e.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("openai embeddings returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var parsed struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, err
	}
	if len(parsed.Data) == 0 || len(parsed.Data[0].Embedding) == 0 {
		return nil, fmt.Errorf("openai embeddings response contained no vector")
	}
	return parsed.Data[0].Embedding, nil
}

type cacheRequest struct {
	prompt     string
	model      string
	systemHash string
	paramsHash string
	scopeHash  string
	stream     bool
}

func extractRequest(r *http.Request) (cacheRequest, []byte, bool) {
	if r.Method != http.MethodPost {
		return cacheRequest{}, nil, false
	}
	if !strings.Contains(strings.ToLower(r.Header.Get("Content-Type")), "application/json") {
		return cacheRequest{}, nil, false
	}
	if r.Body == nil || r.Body == http.NoBody {
		return cacheRequest{}, nil, false
	}

	body, err := io.ReadAll(r.Body)
	_ = r.Body.Close()
	if err != nil || len(bytes.TrimSpace(body)) == 0 {
		return cacheRequest{}, body, false
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return cacheRequest{}, body, false
	}

	prompt, system := extractPromptAndSystem(fields)
	prompt = strings.TrimSpace(prompt)
	if prompt == "" {
		return cacheRequest{}, body, false
	}

	model := jsonString(fields["model"])
	systemHash := hashString(strings.Join(system, "\n"))
	paramsHash := hashJSONFields(fields, map[string]struct{}{
		"messages": {},
		"model":    {},
		"prompt":   {},
		"stream":   {},
	})
	stream := jsonBool(fields["stream"])
	scopeHash := hashScope(map[string]string{
		"method":      r.Method,
		"path":        r.URL.EscapedPath(),
		"model":       model,
		"system_hash": systemHash,
		"params_hash": paramsHash,
		"stream":      strconv.FormatBool(stream),
	})

	return cacheRequest{
		prompt:     prompt,
		model:      model,
		systemHash: systemHash,
		paramsHash: paramsHash,
		scopeHash:  scopeHash,
		stream:     stream,
	}, body, true
}

func restoreRequestBody(r *http.Request, body []byte) {
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

func extractPromptAndSystem(fields map[string]json.RawMessage) (string, []string) {
	if rawPrompt, ok := fields["prompt"]; ok {
		var value interface{}
		if err := json.Unmarshal(rawPrompt, &value); err == nil {
			return contentText(value), nil
		}
	}

	var messages []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if err := json.Unmarshal(fields["messages"], &messages); err != nil {
		return "", nil
	}

	var systemParts []string
	var userParts []string
	var fallbackParts []string
	for _, msg := range messages {
		var value interface{}
		if err := json.Unmarshal(msg.Content, &value); err != nil {
			continue
		}
		text := strings.TrimSpace(contentText(value))
		if text == "" {
			continue
		}
		switch msg.Role {
		case "system", "developer":
			systemParts = append(systemParts, text)
		case "user":
			userParts = append(userParts, text)
		default:
			fallbackParts = append(fallbackParts, text)
		}
	}

	if len(userParts) > 0 {
		return strings.Join(userParts, "\n"), systemParts
	}
	return strings.Join(fallbackParts, "\n"), systemParts
}

func contentText(value interface{}) string {
	switch typed := value.(type) {
	case string:
		return typed
	case []interface{}:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			text := strings.TrimSpace(contentText(item))
			if text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	case map[string]interface{}:
		if rawText, ok := typed["text"]; ok {
			if text, ok := rawText.(string); ok {
				return text
			}
		}
		if rawContent, ok := typed["content"]; ok {
			return contentText(rawContent)
		}
		return ""
	default:
		return ""
	}
}

func jsonString(raw json.RawMessage) string {
	var value string
	if len(raw) == 0 || json.Unmarshal(raw, &value) != nil {
		return ""
	}
	return value
}

func jsonBool(raw json.RawMessage) bool {
	var value bool
	return len(raw) > 0 && json.Unmarshal(raw, &value) == nil && value
}

func hashJSONFields(fields map[string]json.RawMessage, excluded map[string]struct{}) string {
	normalized := make(map[string]interface{})
	for key, raw := range fields {
		if _, skip := excluded[key]; skip {
			continue
		}
		var value interface{}
		if err := json.Unmarshal(raw, &value); err != nil {
			continue
		}
		normalized[key] = value
	}
	if len(normalized) == 0 {
		return hashString("")
	}
	body, err := json.Marshal(normalized)
	if err != nil {
		return hashString("")
	}
	return hashString(string(body))
}

func hashScope(values map[string]string) string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	var b strings.Builder
	for _, key := range keys {
		b.WriteString(key)
		b.WriteByte('=')
		b.WriteString(values[key])
		b.WriteByte('\n')
	}
	return hashString(b.String())
}

func hashString(value string) string {
	sum := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", sum[:])
}

type captureResponseWriter struct {
	http.ResponseWriter
	status   int
	body     bytes.Buffer
	writeErr error
}

func newCaptureResponseWriter(w http.ResponseWriter) *captureResponseWriter {
	return &captureResponseWriter{ResponseWriter: w}
}

func (w *captureResponseWriter) WriteHeader(statusCode int) {
	if w.status != 0 {
		return
	}
	w.status = statusCode
	w.ResponseWriter.WriteHeader(statusCode)
}

func (w *captureResponseWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(p)
	if n > 0 {
		_, _ = w.body.Write(p[:n])
	}
	if err != nil {
		w.writeErr = err
	}
	return n, err
}

func (w *captureResponseWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *captureResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (w *captureResponseWriter) statusCode() int {
	if w.status == 0 {
		return http.StatusOK
	}
	return w.status
}

func (w *captureResponseWriter) bodyBytes() []byte {
	return bytes.Clone(w.body.Bytes())
}

func vectorBlob(vector []float64) []byte {
	buf := make([]byte, len(vector)*4)
	for i, value := range vector {
		binary.LittleEndian.PutUint32(buf[i*4:], math.Float32bits(float32(value)))
	}
	return buf
}

func isUnknownIndexError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "unknown index") ||
		strings.Contains(message, "no such index") ||
		strings.Contains(message, "index does not exist")
}

func isIndexExistsError(err error) bool {
	if err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "index already exists") ||
		strings.Contains(message, "already exists")
}

func envString(name string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envFloat64(name string, fallback float64) (float64, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return value, nil
}

func envDuration(name string, fallback time.Duration) (time.Duration, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback, nil
	}
	value, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return value, nil
}

var errMissingVector = errors.New("semantic cache entry is missing a vector")
