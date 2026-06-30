// Package gateway contains the CoreMesh edge proxy for rate limiting and
// provider resilience.
package gateway

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/coremesh/gateway-proxy/internal/cache"
	"github.com/redis/go-redis/v9"
)

const (
	defaultPrimaryURL           = "http://localhost:8000"
	defaultFallbackURL          = "http://localhost:8000"
	defaultRedisURL             = "redis://localhost:6379"
	defaultRateLimitCapacity    = int64(100)
	defaultRateLimitRefill      = float64(20)
	defaultCircuitThreshold     = 5
	defaultCircuitFailureWindow = 30 * time.Second
	defaultCircuitOpenDuration  = 30 * time.Second
	defaultHalfOpenProbeTimeout = 30 * time.Second
	defaultRedisConnectTimeout  = 5 * time.Second
	rateLimitHeaderRemaining    = "x-ratelimit-remaining"
	coremeshRouteHeader         = "x-coremesh-route"
	coremeshCircuitStateHeader  = "x-coremesh-circuit-state"
	rateLimitRedisKeyPrefix     = "coremesh:gateway:ratelimit:"
	rateLimitRedisMinimumTTL    = 60 * time.Second
	anonymousRateLimitIdentity  = "anonymous"
)

// Config controls the gateway reverse proxy, Redis token bucket, and circuit
// breaker behavior.
type Config struct {
	PrimaryURL               string
	FallbackURL              string
	RedisURL                 string
	RateLimitCapacity        int64
	RateLimitRefillPerSecond float64
	CircuitFailureThreshold  int
	CircuitFailureWindow     time.Duration
	CircuitOpenDuration      time.Duration
}

// DefaultConfig returns local-development defaults for the gateway.
func DefaultConfig() Config {
	return Config{
		PrimaryURL:               defaultPrimaryURL,
		FallbackURL:              defaultFallbackURL,
		RedisURL:                 defaultRedisURL,
		RateLimitCapacity:        defaultRateLimitCapacity,
		RateLimitRefillPerSecond: defaultRateLimitRefill,
		CircuitFailureThreshold:  defaultCircuitThreshold,
		CircuitFailureWindow:     defaultCircuitFailureWindow,
		CircuitOpenDuration:      defaultCircuitOpenDuration,
	}
}

// ConfigFromEnv loads gateway settings from environment variables.
func ConfigFromEnv() (Config, error) {
	cfg := DefaultConfig()

	cfg.PrimaryURL = envString("GATEWAY_PRIMARY_URL", cfg.PrimaryURL)
	cfg.FallbackURL = envString("GATEWAY_FALLBACK_URL", cfg.FallbackURL)
	cfg.RedisURL = envString("REDIS_URL", cfg.RedisURL)

	var err error
	if cfg.RateLimitCapacity, err = envInt64("RATE_LIMIT_CAPACITY", cfg.RateLimitCapacity); err != nil {
		return Config{}, err
	}
	if cfg.RateLimitRefillPerSecond, err = envFloat64("RATE_LIMIT_REFILL_PER_SECOND", cfg.RateLimitRefillPerSecond); err != nil {
		return Config{}, err
	}
	if cfg.CircuitFailureThreshold, err = envInt("CIRCUIT_FAILURE_THRESHOLD", cfg.CircuitFailureThreshold); err != nil {
		return Config{}, err
	}
	if cfg.CircuitFailureWindow, err = envDuration("CIRCUIT_FAILURE_WINDOW", cfg.CircuitFailureWindow); err != nil {
		return Config{}, err
	}
	if cfg.CircuitOpenDuration, err = envDuration("CIRCUIT_OPEN_DURATION", cfg.CircuitOpenDuration); err != nil {
		return Config{}, err
	}

	cfg = cfg.withDefaults()
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate checks the gateway configuration before startup.
func (c Config) Validate() error {
	if c.RateLimitCapacity <= 0 {
		return fmt.Errorf("RATE_LIMIT_CAPACITY must be greater than zero")
	}
	if c.RateLimitRefillPerSecond <= 0 {
		return fmt.Errorf("RATE_LIMIT_REFILL_PER_SECOND must be greater than zero")
	}
	if c.CircuitFailureThreshold <= 0 {
		return fmt.Errorf("CIRCUIT_FAILURE_THRESHOLD must be greater than zero")
	}
	if c.CircuitFailureWindow <= 0 {
		return fmt.Errorf("CIRCUIT_FAILURE_WINDOW must be greater than zero")
	}
	if c.CircuitOpenDuration <= 0 {
		return fmt.Errorf("CIRCUIT_OPEN_DURATION must be greater than zero")
	}
	if _, err := url.ParseRequestURI(c.PrimaryURL); err != nil {
		return fmt.Errorf("invalid GATEWAY_PRIMARY_URL: %w", err)
	}
	if _, err := url.ParseRequestURI(c.FallbackURL); err != nil {
		return fmt.Errorf("invalid GATEWAY_FALLBACK_URL: %w", err)
	}
	return nil
}

func (c Config) withDefaults() Config {
	defaults := DefaultConfig()
	if c.PrimaryURL == "" {
		c.PrimaryURL = defaults.PrimaryURL
	}
	if c.FallbackURL == "" {
		c.FallbackURL = defaults.FallbackURL
	}
	if c.RedisURL == "" {
		c.RedisURL = defaults.RedisURL
	}
	if c.RateLimitCapacity == 0 {
		c.RateLimitCapacity = defaults.RateLimitCapacity
	}
	if c.RateLimitRefillPerSecond == 0 {
		c.RateLimitRefillPerSecond = defaults.RateLimitRefillPerSecond
	}
	if c.CircuitFailureThreshold == 0 {
		c.CircuitFailureThreshold = defaults.CircuitFailureThreshold
	}
	if c.CircuitFailureWindow == 0 {
		c.CircuitFailureWindow = defaults.CircuitFailureWindow
	}
	if c.CircuitOpenDuration == 0 {
		c.CircuitOpenDuration = defaults.CircuitOpenDuration
	}
	return c
}

// LimitResult is the result of one token-bucket admission check.
type LimitResult struct {
	Allowed    bool
	Remaining  int64
	RetryAfter time.Duration
}

// RateLimiter is implemented by the Redis token bucket and test fakes.
type RateLimiter interface {
	Allow(ctx context.Context, key string) (LimitResult, error)
}

// RedisTokenBucket enforces distributed, atomic token-bucket rate limits.
type RedisTokenBucket struct {
	client          *redis.Client
	capacity        int64
	refillPerSecond float64
	ttl             time.Duration
}

// NewRedisTokenBucket creates a Redis-backed token bucket limiter.
func NewRedisTokenBucket(client *redis.Client, capacity int64, refillPerSecond float64) *RedisTokenBucket {
	ttl := time.Duration(math.Ceil(float64(capacity)/refillPerSecond)*2) * time.Second
	if ttl < rateLimitRedisMinimumTTL {
		ttl = rateLimitRedisMinimumTTL
	}

	return &RedisTokenBucket{
		client:          client,
		capacity:        capacity,
		refillPerSecond: refillPerSecond,
		ttl:             ttl,
	}
}

// Allow consumes one request token if available.
func (b *RedisTokenBucket) Allow(ctx context.Context, key string) (LimitResult, error) {
	identity := strings.TrimSpace(key)
	if identity == "" {
		identity = anonymousRateLimitIdentity
	}

	raw, err := tokenBucketScript.Run(
		ctx,
		b.client,
		[]string{rateLimitRedisKeyPrefix + identity},
		b.capacity,
		strconv.FormatFloat(b.refillPerSecond, 'f', 6, 64),
		int64(b.ttl.Seconds()),
	).Result()
	if err != nil {
		return LimitResult{}, err
	}

	values, ok := raw.([]interface{})
	if !ok || len(values) != 3 {
		return LimitResult{}, fmt.Errorf("unexpected Redis token bucket result: %T", raw)
	}

	allowed, err := redisInt64(values[0])
	if err != nil {
		return LimitResult{}, err
	}
	remaining, err := redisInt64(values[1])
	if err != nil {
		return LimitResult{}, err
	}
	retryAfterSeconds, err := redisInt64(values[2])
	if err != nil {
		return LimitResult{}, err
	}

	return LimitResult{
		Allowed:    allowed == 1,
		Remaining:  remaining,
		RetryAfter: time.Duration(retryAfterSeconds) * time.Second,
	}, nil
}

var tokenBucketScript = redis.NewScript(`
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local redis_time = redis.call("TIME")
local now = tonumber(redis_time[1]) + (tonumber(redis_time[2]) / 1000000)

local state = redis.call("HMGET", KEYS[1], "tokens", "updated_at")
local tokens = tonumber(state[1])
local updated_at = tonumber(state[2])

if tokens == nil then
  tokens = capacity
end
if updated_at == nil then
  updated_at = now
end

local elapsed = math.max(0, now - updated_at)
tokens = math.min(capacity, tokens + (elapsed * refill_rate))

local allowed = 0
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = math.ceil((1 - tokens) / refill_rate)
end

redis.call("HSET", KEYS[1], "tokens", tokens, "updated_at", now)
redis.call("EXPIRE", KEYS[1], ttl)

return {allowed, math.floor(tokens), retry_after}
`)

// Proxy is the concurrent HTTP reverse proxy with rate limiting and resilience.
type Proxy struct {
	cfg           Config
	limiter       RateLimiter
	breaker       *CircuitBreaker
	primaryProxy  *httputil.ReverseProxy
	fallbackProxy *httputil.ReverseProxy
}

// NewHandler builds a production gateway handler and verifies Redis access.
func NewHandler(ctx context.Context, cfg Config) (http.Handler, error) {
	cfg = cfg.withDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}

	redisOptions, err := redis.ParseURL(cfg.RedisURL)
	if err != nil {
		return nil, fmt.Errorf("invalid REDIS_URL: %w", err)
	}
	redisOptions.Protocol = 2

	redisClient := redis.NewClient(redisOptions)
	pingCtx, cancel := context.WithTimeout(ctx, defaultRedisConnectTimeout)
	defer cancel()
	if err := redisClient.Ping(pingCtx).Err(); err != nil {
		_ = redisClient.Close()
		return nil, fmt.Errorf("redis ping failed: %w", err)
	}

	proxy, err := NewProxy(cfg, NewRedisTokenBucket(redisClient, cfg.RateLimitCapacity, cfg.RateLimitRefillPerSecond))
	if err != nil {
		_ = redisClient.Close()
		return nil, err
	}

	cacheCfg, err := cache.ConfigFromEnv()
	if err != nil {
		_ = redisClient.Close()
		return nil, err
	}
	if !cacheCfg.Enabled {
		return proxy, nil
	}

	embedder, err := cache.NewOpenAIEmbedder(cacheCfg)
	if err != nil {
		_ = redisClient.Close()
		return nil, err
	}
	semanticCache, err := cache.NewSemanticCache(cacheCfg, cache.NewRedisStore(redisClient, cacheCfg), embedder)
	if err != nil {
		_ = redisClient.Close()
		return nil, err
	}
	return semanticCache.Middleware(proxy), nil
}

// NewProxy builds a gateway proxy with an injected limiter, which keeps tests
// independent from Redis.
func NewProxy(cfg Config, limiter RateLimiter) (*Proxy, error) {
	cfg = cfg.withDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if limiter == nil {
		return nil, fmt.Errorf("rate limiter is required")
	}

	primaryURL, err := url.Parse(cfg.PrimaryURL)
	if err != nil {
		return nil, fmt.Errorf("parse primary URL: %w", err)
	}
	fallbackURL, err := url.Parse(cfg.FallbackURL)
	if err != nil {
		return nil, fmt.Errorf("parse fallback URL: %w", err)
	}

	proxy := &Proxy{
		cfg:     cfg,
		limiter: limiter,
		breaker: NewCircuitBreaker(
			cfg.CircuitFailureThreshold,
			cfg.CircuitFailureWindow,
			cfg.CircuitOpenDuration,
		),
	}
	proxy.primaryProxy = proxy.newPrimaryProxy(primaryURL)
	proxy.fallbackProxy = proxy.newFallbackProxy(fallbackURL)

	return proxy, nil
}

// ServeHTTP applies rate limiting, chooses the active backend, and proxies the
// request.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	limit, err := p.limiter.Allow(r.Context(), rateLimitKey(r))
	if err != nil {
		http.Error(w, "rate limiter unavailable", http.StatusServiceUnavailable)
		return
	}

	w.Header().Set(rateLimitHeaderRemaining, strconv.FormatInt(limit.Remaining, 10))
	if !limit.Allowed {
		w.Header().Set("Retry-After", strconv.FormatInt(retryAfterSeconds(limit.RetryAfter), 10))
		http.Error(w, "too many requests", http.StatusTooManyRequests)
		return
	}

	route := p.breaker.Route(time.Now())
	w.Header().Set(coremeshCircuitStateHeader, route.State)
	if route.UseFallback {
		p.fallbackProxy.ServeHTTP(w, r)
		return
	}

	request, err := cloneWithReplayableBody(r)
	if err != nil {
		http.Error(w, "failed to read request body", http.StatusBadRequest)
		return
	}
	p.primaryProxy.ServeHTTP(w, request)
}

// CircuitState returns the current circuit state for diagnostics and tests.
func (p *Proxy) CircuitState() string {
	return p.breaker.State()
}

func (p *Proxy) newPrimaryProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := newSingleHostProxy(target)
	proxy.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Set(coremeshRouteHeader, "primary")
		if resp.StatusCode >= http.StatusInternalServerError {
			p.breaker.RecordFailure(time.Now())
			return &primaryFailureError{statusCode: resp.StatusCode}
		}

		p.breaker.RecordSuccess(time.Now())
		return nil
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		var primaryErr *primaryFailureError
		if !errors.As(err, &primaryErr) {
			p.breaker.RecordFailure(time.Now())
		}

		if p.breaker.useFallbackAfterPrimaryError() {
			resetReplayableBody(r)
			p.fallbackProxy.ServeHTTP(w, r)
			return
		}

		statusCode := http.StatusBadGateway
		message := "primary upstream unavailable"
		if primaryErr != nil {
			statusCode = primaryErr.statusCode
			message = primaryErr.Error()
		}
		http.Error(w, message, statusCode)
	}
	return proxy
}

func (p *Proxy) newFallbackProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := newSingleHostProxy(target)
	proxy.ModifyResponse = func(resp *http.Response) error {
		resp.Header.Set(coremeshRouteHeader, "fallback")
		return nil
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, _ error) {
		http.Error(w, "fallback upstream unavailable", http.StatusBadGateway)
	}
	return proxy
}

func newSingleHostProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := httputil.NewSingleHostReverseProxy(target)
	director := proxy.Director
	proxy.Director = func(r *http.Request) {
		originalHost := r.Host
		director(r)
		r.Host = target.Host
		if originalHost != "" {
			r.Header.Set("X-Forwarded-Host", originalHost)
		}
	}
	return proxy
}

type circuitState string

const (
	circuitClosed   circuitState = "closed"
	circuitOpen     circuitState = "open"
	circuitHalfOpen circuitState = "half-open"
)

type routeDecision struct {
	UseFallback bool
	State       string
}

// CircuitBreaker tracks primary upstream failures over a rolling window.
type CircuitBreaker struct {
	mu                   sync.Mutex
	state                circuitState
	failureThreshold     int
	failureWindow        time.Duration
	openDuration         time.Duration
	halfOpenProbeTimeout time.Duration
	failures             []time.Time
	openedAt             time.Time
	halfOpenInFlight     bool
	halfOpenStartedAt    time.Time
}

// NewCircuitBreaker creates a rolling-window circuit breaker.
func NewCircuitBreaker(threshold int, failureWindow time.Duration, openDuration time.Duration) *CircuitBreaker {
	return &CircuitBreaker{
		state:                circuitClosed,
		failureThreshold:     threshold,
		failureWindow:        failureWindow,
		openDuration:         openDuration,
		halfOpenProbeTimeout: defaultHalfOpenProbeTimeout,
	}
}

// Route determines whether the next request should hit primary or fallback.
func (b *CircuitBreaker) Route(now time.Time) routeDecision {
	b.mu.Lock()
	defer b.mu.Unlock()

	switch b.state {
	case circuitOpen:
		if now.Sub(b.openedAt) >= b.openDuration && !b.halfOpenInFlight {
			b.state = circuitHalfOpen
			b.halfOpenInFlight = true
			b.halfOpenStartedAt = now
			return routeDecision{State: string(b.state)}
		}
		return routeDecision{UseFallback: true, State: string(b.state)}
	case circuitHalfOpen:
		b.expireHalfOpenProbeIfStale(now)
		if b.halfOpenInFlight {
			return routeDecision{UseFallback: true, State: string(b.state)}
		}
		b.halfOpenInFlight = true
		b.halfOpenStartedAt = now
		return routeDecision{State: string(b.state)}
	default:
		return routeDecision{State: string(b.state)}
	}
}

// RecordSuccess closes a half-open circuit after a successful probe.
func (b *CircuitBreaker) RecordSuccess(now time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.state == circuitHalfOpen {
		b.state = circuitClosed
		b.failures = nil
		b.halfOpenInFlight = false
		b.halfOpenStartedAt = time.Time{}
		return
	}

	b.failures = pruneFailures(b.failures, now, b.failureWindow)
}

// RecordFailure records a primary provider failure and opens the circuit when
// the threshold is crossed inside the configured rolling window.
func (b *CircuitBreaker) RecordFailure(now time.Time) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.failures = append(pruneFailures(b.failures, now, b.failureWindow), now)

	if b.state == circuitHalfOpen {
		b.open(now)
		return
	}

	if b.state == circuitClosed && len(b.failures) >= b.failureThreshold {
		b.open(now)
	}
}

// State returns the current circuit state.
func (b *CircuitBreaker) State() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.state)
}

func (b *CircuitBreaker) open(now time.Time) {
	b.state = circuitOpen
	b.openedAt = now
	b.halfOpenInFlight = false
	b.halfOpenStartedAt = time.Time{}
}

func (b *CircuitBreaker) useFallbackAfterPrimaryError() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.state == circuitOpen
}

func (b *CircuitBreaker) expireHalfOpenProbeIfStale(now time.Time) {
	if !b.halfOpenInFlight || b.halfOpenStartedAt.IsZero() {
		return
	}
	if now.Sub(b.halfOpenStartedAt) < b.halfOpenProbeTimeout {
		return
	}
	b.halfOpenInFlight = false
	b.halfOpenStartedAt = time.Time{}
}

func pruneFailures(failures []time.Time, now time.Time, window time.Duration) []time.Time {
	cutoff := now.Add(-window)
	firstKept := 0
	for firstKept < len(failures) && failures[firstKept].Before(cutoff) {
		firstKept++
	}
	return failures[firstKept:]
}

type primaryFailureError struct {
	statusCode int
}

func (e *primaryFailureError) Error() string {
	return fmt.Sprintf("primary upstream returned %d", e.statusCode)
}

type replayableBodyKey struct{}

func cloneWithReplayableBody(r *http.Request) (*http.Request, error) {
	if r.Body == nil || r.Body == http.NoBody {
		return r, nil
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	_ = r.Body.Close()

	clone := r.WithContext(context.WithValue(r.Context(), replayableBodyKey{}, body))
	clone.Body = io.NopCloser(bytes.NewReader(body))
	clone.ContentLength = int64(len(body))
	clone.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
	return clone, nil
}

func resetReplayableBody(r *http.Request) {
	body, ok := r.Context().Value(replayableBodyKey{}).([]byte)
	if !ok {
		return
	}

	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

func rateLimitKey(r *http.Request) string {
	if teamID := strings.TrimSpace(r.Header.Get("X-Team-ID")); teamID != "" {
		return teamID
	}
	if apiKey := strings.TrimSpace(r.Header.Get("X-API-Key")); apiKey != "" {
		return apiKey
	}

	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil && host != "" {
		return host
	}
	if r.RemoteAddr != "" {
		return r.RemoteAddr
	}
	return anonymousRateLimitIdentity
}

func retryAfterSeconds(d time.Duration) int64 {
	if d <= 0 {
		return 1
	}
	seconds := int64(math.Ceil(d.Seconds()))
	if seconds < 1 {
		return 1
	}
	return seconds
}

func redisInt64(value interface{}) (int64, error) {
	switch v := value.(type) {
	case int64:
		return v, nil
	case int:
		return int64(v), nil
	case string:
		return strconv.ParseInt(v, 10, 64)
	case []byte:
		return strconv.ParseInt(string(v), 10, 64)
	default:
		return 0, fmt.Errorf("unexpected Redis integer value %T", value)
	}
}

func envString(name string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) (int, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return value, nil
}

func envInt64(name string, fallback int64) (int64, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback, nil
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return value, nil
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
