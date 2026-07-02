// Package autopilot contains request-time model routing and experiment splits.
package autopilot

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
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	defaultTier1Model      = "gpt-4o-mini"
	defaultTier3Model      = "gpt-4o"
	defaultExperimentFlag  = "cost_autopilot_routing"
	defaultAutopilotEnable = true

	Tier1 = "tier-1"
	Tier3 = "tier-3"

	VariantNone         = "none"
	VariantBaseline     = "baseline"
	VariantExperimental = "experimental"

	defaultExperimentLookupTimeout = 2 * time.Second

	CachePolicyAllow  = "allow"
	CachePolicyBypass = "bypass"

	HeaderAutopilotTier      = "X-CoreMesh-Autopilot-Tier"
	HeaderRoutedModel        = "X-CoreMesh-Routed-Model"
	HeaderExperimentVariant  = "X-CoreMesh-Experiment-Variant"
	HeaderPromptVersion      = "X-CoreMesh-Prompt-Version"
	HeaderCachePolicy        = "X-CoreMesh-Cache-Policy"
	HeaderAutopilotReason    = "X-CoreMesh-Autopilot-Reason"
	HeaderExperimentError    = "X-CoreMesh-Experiment-Error"
	anonymousRoutingIdentity = "anonymous"
)

// Config controls request classification and experiment-split behavior.
type Config struct {
	Enabled                 bool
	Tier1Model              string
	Tier3Model              string
	ExperimentFlag          string
	PostgresDSN             string
	ExperimentLookupTimeout time.Duration
	Debug                   bool
}

// DefaultConfig returns production-ready routing defaults.
func DefaultConfig() Config {
	return Config{
		Enabled:                 defaultAutopilotEnable,
		Tier1Model:              defaultTier1Model,
		Tier3Model:              defaultTier3Model,
		ExperimentFlag:          defaultExperimentFlag,
		ExperimentLookupTimeout: defaultExperimentLookupTimeout,
	}
}

// ConfigFromEnv loads autopilot settings from environment variables.
func ConfigFromEnv() (Config, error) {
	cfg := DefaultConfig()

	enabledRaw := strings.TrimSpace(os.Getenv("AUTOPILOT_ENABLED"))
	if enabledRaw != "" {
		enabled, err := strconv.ParseBool(enabledRaw)
		if err != nil {
			return Config{}, fmt.Errorf("invalid AUTOPILOT_ENABLED: %w", err)
		}
		cfg.Enabled = enabled
	}

	cfg.Tier1Model = envString("AUTOPILOT_TIER1_MODEL", cfg.Tier1Model)
	cfg.Tier3Model = envString("AUTOPILOT_TIER3_MODEL", cfg.Tier3Model)
	cfg.ExperimentFlag = envString("AUTOPILOT_EXPERIMENT_FLAG", cfg.ExperimentFlag)
	cfg.PostgresDSN = strings.TrimSpace(os.Getenv("POSTGRES_DSN"))
	cfg.Debug = envBool("AUTOPILOT_DEBUG", cfg.Debug)
	var err error
	if cfg.ExperimentLookupTimeout, err = envDuration("AUTOPILOT_EXPERIMENT_LOOKUP_TIMEOUT", cfg.ExperimentLookupTimeout); err != nil {
		return Config{}, err
	}

	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate checks the autopilot configuration.
func (c Config) Validate() error {
	if !c.Enabled {
		return nil
	}
	if strings.TrimSpace(c.Tier1Model) == "" {
		return fmt.Errorf("AUTOPILOT_TIER1_MODEL is required when autopilot is enabled")
	}
	if strings.TrimSpace(c.Tier3Model) == "" {
		return fmt.Errorf("AUTOPILOT_TIER3_MODEL is required when autopilot is enabled")
	}
	if strings.TrimSpace(c.ExperimentFlag) == "" {
		return fmt.Errorf("AUTOPILOT_EXPERIMENT_FLAG is required when autopilot is enabled")
	}
	if c.ExperimentLookupTimeout <= 0 {
		return fmt.Errorf("AUTOPILOT_EXPERIMENT_LOOKUP_TIMEOUT must be greater than zero")
	}
	return nil
}

func (c Config) withDefaults() Config {
	defaults := DefaultConfig()
	if c.Tier1Model == "" {
		c.Tier1Model = defaults.Tier1Model
	}
	if c.Tier3Model == "" {
		c.Tier3Model = defaults.Tier3Model
	}
	if c.ExperimentFlag == "" {
		c.ExperimentFlag = defaults.ExperimentFlag
	}
	if c.ExperimentLookupTimeout == 0 {
		c.ExperimentLookupTimeout = defaults.ExperimentLookupTimeout
	}
	return c
}

// Experiment mirrors the routing fields in feature_experiments.
type Experiment struct {
	FlagName                  string
	RolloutPercentage         int
	BaselinePromptVersion     int
	ExperimentalPromptVersion int
	Status                    string
}

// ExperimentStore resolves the current experiment configuration for a flag.
type ExperimentStore interface {
	LookupExperiment(ctx context.Context, flagName string) (Experiment, bool, error)
}

// PostgresExperimentStore reads feature_experiments on demand.
type PostgresExperimentStore struct {
	pool          *pgxpool.Pool
	lookupTimeout time.Duration
}

// NewPostgresExperimentStore creates a PostgreSQL-backed experiment store.
func NewPostgresExperimentStore(ctx context.Context, dsn string, lookupTimeout time.Duration) (*PostgresExperimentStore, error) {
	if strings.TrimSpace(dsn) == "" {
		return nil, fmt.Errorf("POSTGRES_DSN is required")
	}
	if lookupTimeout <= 0 {
		lookupTimeout = defaultExperimentLookupTimeout
	}
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	return &PostgresExperimentStore{pool: pool, lookupTimeout: lookupTimeout}, nil
}

// Close releases the underlying PostgreSQL pool.
func (s *PostgresExperimentStore) Close() {
	if s == nil || s.pool == nil {
		return
	}
	s.pool.Close()
}

// LookupExperiment loads a single feature_experiments row.
func (s *PostgresExperimentStore) LookupExperiment(ctx context.Context, flagName string) (Experiment, bool, error) {
	if s == nil || s.pool == nil {
		return Experiment{}, false, fmt.Errorf("postgres experiment pool is required")
	}

	ctx, cancel := context.WithTimeout(ctx, s.lookupTimeout)
	defer cancel()

	var exp Experiment
	err := s.pool.QueryRow(ctx, `
SELECT flag_name, rollout_percentage, baseline_prompt_version, experimental_prompt_version, status
FROM feature_experiments
WHERE flag_name = $1
`, flagName).Scan(
		&exp.FlagName,
		&exp.RolloutPercentage,
		&exp.BaselinePromptVersion,
		&exp.ExperimentalPromptVersion,
		&exp.Status,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Experiment{}, false, nil
	}
	if err != nil {
		return Experiment{}, false, err
	}
	return exp, true, nil
}

// Classification explains the model tier selected from request features.
type Classification struct {
	Tier        string
	Score       int
	TokenCount  int
	CachePolicy string
	Reasons     []string
}

// Decision is the complete routing choice applied to an HTTP request.
type Decision struct {
	Tier          string
	Model         string
	Variant       string
	PromptVersion int
	CachePolicy   string
	Reasons       []string
	DebugError    string
}

// Router classifies LLM requests and optionally evaluates experiment splits.
type Router struct {
	cfg   Config
	store ExperimentStore
}

// NewRouter builds an autopilot router with an optional experiment store.
func NewRouter(cfg Config, store ExperimentStore) (*Router, error) {
	cfg = cfg.withDefaults()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &Router{cfg: cfg, store: store}, nil
}

// Middleware rewrites compatible JSON LLM requests before they reach cache/proxy layers.
func (r *Router) Middleware(next http.Handler) http.Handler {
	if r == nil || !r.cfg.Enabled {
		return next
	}

	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		routed, decision, ok, err := r.routeRequest(req)
		if err != nil {
			http.Error(w, "autopilot routing failed", http.StatusBadRequest)
			return
		}
		if !ok {
			next.ServeHTTP(w, req)
			return
		}

		applyDecisionHeaders(w.Header(), decision)
		applyDecisionHeaders(routed.Header, decision)
		next.ServeHTTP(w, routed)
	})
}

func (r *Router) routeRequest(req *http.Request) (*http.Request, Decision, bool, error) {
	if req.Method != http.MethodPost {
		return req, Decision{}, false, nil
	}
	if !strings.Contains(strings.ToLower(req.Header.Get("Content-Type")), "application/json") {
		return req, Decision{}, false, nil
	}
	if req.Body == nil || req.Body == http.NoBody {
		return req, Decision{}, false, nil
	}

	body, err := io.ReadAll(req.Body)
	_ = req.Body.Close()
	if err != nil {
		return req, Decision{}, false, err
	}
	if len(bytes.TrimSpace(body)) == 0 {
		restoreRequestBody(req, body)
		return req, Decision{}, false, nil
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		restoreRequestBody(req, body)
		return req, Decision{}, false, nil
	}
	if strings.TrimSpace(extractPromptText(fields)) == "" {
		restoreRequestBody(req, body)
		return req, Decision{}, false, nil
	}

	classification := ClassifyFields(fields)
	decision := r.decision(req.Context(), req, classification)
	routedBody, err := rewriteModel(fields, decision.Model)
	if err != nil {
		restoreRequestBody(req, body)
		return req, Decision{}, false, err
	}

	routed := req.Clone(req.Context())
	restoreRequestBody(routed, routedBody)
	return routed, decision, true, nil
}

func (r *Router) decision(ctx context.Context, req *http.Request, classification Classification) Decision {
	decision := Decision{
		Tier:        classification.Tier,
		Model:       r.modelForTier(classification.Tier),
		Variant:     VariantNone,
		CachePolicy: classification.CachePolicy,
		Reasons:     append([]string(nil), classification.Reasons...),
	}

	if r.store == nil {
		return decision
	}

	exp, found, err := r.store.LookupExperiment(ctx, r.cfg.ExperimentFlag)
	if err != nil {
		decision = r.baselineDecision(decision, 0, "experiment_lookup_failed")
		if r.cfg.Debug {
			decision.DebugError = sanitizeHeaderValue(err.Error())
		}
		return decision
	}
	if !found {
		return decision
	}

	switch strings.ToLower(strings.TrimSpace(exp.Status)) {
	case "running":
		if userInRollout(r.cfg.ExperimentFlag, userIdentity(req), exp.RolloutPercentage) {
			decision.Variant = VariantExperimental
			decision.PromptVersion = exp.ExperimentalPromptVersion
			decision.Reasons = appendReason(decision.Reasons, "experiment_experimental")
			return decision
		}
		return r.baselineDecision(decision, exp.BaselinePromptVersion, "experiment_baseline")
	case "rolled_back":
		return r.baselineDecision(decision, exp.BaselinePromptVersion, "experiment_rolled_back")
	default:
		return decision
	}
}

func (r *Router) baselineDecision(decision Decision, promptVersion int, reason string) Decision {
	decision.Tier = Tier3
	decision.Model = r.cfg.Tier3Model
	decision.Variant = VariantBaseline
	decision.PromptVersion = promptVersion
	decision.Reasons = appendReason(decision.Reasons, reason)
	return decision
}

func (r *Router) modelForTier(tier string) string {
	if tier == Tier3 {
		return r.cfg.Tier3Model
	}
	return r.cfg.Tier1Model
}

// ClassifyFields scores a JSON LLM payload and maps it to a model tier.
func ClassifyFields(fields map[string]json.RawMessage) Classification {
	prompt := extractPromptText(fields)
	tokenCount := approximateTokenCount(prompt)

	score := 0
	var reasons []string

	if tokenCount > 180 {
		score += 2
		reasons = append(reasons, "long_prompt")
	}
	if tokenCount > 400 {
		score += 2
		reasons = append(reasons, "very_long_prompt")
	}

	codeMarkers := countMatches(prompt, []string{
		"```", "traceback", "stack trace", "panic:", "exception", "nullpointer",
		"segmentation fault", "func ", "def ", "class ", "package ", "import ",
		"async ", "await ", "select ", " join ", "where ", "dockerfile",
	})
	if codeMarkers > 0 {
		score += 3
		reasons = append(reasons, "code_or_error_markers")
	}

	complexKeywords := countMatches(prompt, []string{
		"analyze", "compare", "reason", "debug", "diagnose", "root cause",
		"optimize", "refactor", "architecture", "tradeoff", "multi-step",
		"step by step", "algorithm", "proof", "derive", "evaluate", "risk",
		"concurrency", "distributed", "transaction", "migration", "security",
	})
	if complexKeywords >= 1 {
		score++
		reasons = append(reasons, "complexity_keywords")
	}
	if complexKeywords >= 3 {
		score++
		reasons = append(reasons, "multiple_complexity_keywords")
	}

	constraints := countConstraints(prompt)
	if constraints >= 4 {
		score++
		reasons = append(reasons, "multiple_constraints")
	}
	if constraints >= 8 {
		score++
		reasons = append(reasons, "many_constraints")
	}

	if hasNonEmptyJSON(fields["tools"]) || hasNonEmptyJSON(fields["functions"]) {
		score += 3
		reasons = append(reasons, "tool_calling")
	}
	if responseFormatIsComplex(fields["response_format"]) {
		score += 2
		reasons = append(reasons, "complex_response_format")
	}
	if maxTokens(fields["max_tokens"]) > 1500 {
		score += 2
		reasons = append(reasons, "large_max_tokens")
	}
	if temperature(fields["temperature"]) > 0.8 {
		score++
		reasons = append(reasons, "high_temperature")
	}

	tier := Tier1
	cachePolicy := CachePolicyAllow
	if score >= 3 {
		tier = Tier3
		cachePolicy = CachePolicyBypass
	}
	if len(reasons) == 0 {
		reasons = append(reasons, "simple_request")
	}

	return Classification{
		Tier:        tier,
		Score:       score,
		TokenCount:  tokenCount,
		CachePolicy: cachePolicy,
		Reasons:     reasons,
	}
}

func applyDecisionHeaders(headers http.Header, decision Decision) {
	headers.Set(HeaderAutopilotTier, decision.Tier)
	headers.Set(HeaderRoutedModel, decision.Model)
	headers.Set(HeaderExperimentVariant, decision.Variant)
	headers.Set(HeaderCachePolicy, decision.CachePolicy)
	if decision.PromptVersion > 0 {
		headers.Set(HeaderPromptVersion, strconv.Itoa(decision.PromptVersion))
	}
	if len(decision.Reasons) > 0 {
		headers.Set(HeaderAutopilotReason, strings.Join(decision.Reasons, ","))
	}
	if strings.TrimSpace(decision.DebugError) != "" {
		headers.Set(HeaderExperimentError, decision.DebugError)
	}
}

func rewriteModel(fields map[string]json.RawMessage, model string) ([]byte, error) {
	rawModel, err := json.Marshal(model)
	if err != nil {
		return nil, err
	}
	fields["model"] = rawModel
	return json.Marshal(fields)
}

func extractPromptText(fields map[string]json.RawMessage) string {
	var parts []string
	for _, key := range []string{"prompt", "input"} {
		if raw, ok := fields[key]; ok {
			var value interface{}
			if err := json.Unmarshal(raw, &value); err == nil {
				text := strings.TrimSpace(contentText(value))
				if text != "" {
					parts = append(parts, text)
				}
			}
		}
	}

	var messages []struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	}
	if err := json.Unmarshal(fields["messages"], &messages); err == nil {
		for _, msg := range messages {
			if msg.Role != "" && msg.Role != "user" && msg.Role != "system" && msg.Role != "developer" {
				continue
			}
			var value interface{}
			if err := json.Unmarshal(msg.Content, &value); err != nil {
				continue
			}
			text := strings.TrimSpace(contentText(value))
			if text != "" {
				parts = append(parts, text)
			}
		}
	}
	return strings.Join(parts, "\n")
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

func approximateTokenCount(text string) int {
	text = strings.TrimSpace(text)
	if text == "" {
		return 0
	}
	byChars := int(math.Ceil(float64(len([]rune(text))) / 4.0))
	byWords := len(strings.Fields(text))
	if byChars > byWords {
		return byChars
	}
	return byWords
}

func countMatches(text string, needles []string) int {
	lower := strings.ToLower(text)
	count := 0
	for _, needle := range needles {
		if strings.Contains(lower, needle) {
			count++
		}
	}
	return count
}

func countConstraints(text string) int {
	lower := strings.ToLower(text)
	count := countMatches(lower, []string{
		"must", "should", "include", "exclude", "avoid", "only", "exactly",
		"schema", "json", "criteria", "requirement", "without", "ensure",
	})
	for _, line := range strings.Split(lower, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "- ") || strings.HasPrefix(trimmed, "* ") {
			count++
			continue
		}
		if len(trimmed) > 2 && trimmed[0] >= '0' && trimmed[0] <= '9' && (trimmed[1] == '.' || trimmed[1] == ')') {
			count++
		}
	}
	return count
}

func hasNonEmptyJSON(raw json.RawMessage) bool {
	if len(raw) == 0 {
		return false
	}
	var value interface{}
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return false
	}
	switch typed := value.(type) {
	case []interface{}:
		return len(typed) > 0
	case map[string]interface{}:
		return len(typed) > 0
	case string:
		return strings.TrimSpace(typed) != ""
	default:
		return true
	}
}

func responseFormatIsComplex(raw json.RawMessage) bool {
	if len(raw) == 0 {
		return false
	}
	var value map[string]interface{}
	if err := json.Unmarshal(raw, &value); err != nil || len(value) == 0 {
		return false
	}
	if value["json_schema"] != nil || value["schema"] != nil {
		return true
	}
	if rawType, ok := value["type"].(string); ok {
		return strings.Contains(strings.ToLower(rawType), "json")
	}
	return true
}

func maxTokens(raw json.RawMessage) int {
	if len(raw) == 0 {
		return 0
	}
	var value float64
	if err := json.Unmarshal(raw, &value); err != nil {
		return 0
	}
	return int(value)
}

func temperature(raw json.RawMessage) float64 {
	if len(raw) == 0 {
		return 0
	}
	var value float64
	if err := json.Unmarshal(raw, &value); err != nil {
		return 0
	}
	return value
}

func userInRollout(flagName string, identity string, rolloutPercentage int) bool {
	if rolloutPercentage <= 0 {
		return false
	}
	if rolloutPercentage >= 100 {
		return true
	}
	return stableBucket(flagName, identity) < rolloutPercentage
}

func stableBucket(flagName string, identity string) int {
	if strings.TrimSpace(identity) == "" {
		identity = anonymousRoutingIdentity
	}
	sum := sha256.Sum256([]byte(flagName + "\x00" + identity))
	return int(binary.BigEndian.Uint64(sum[:8]) % 100)
}

func userIdentity(r *http.Request) string {
	for _, header := range []string{"X-User-ID", "X-Session-ID", "X-Team-ID", "X-API-Key"} {
		if value := strings.TrimSpace(r.Header.Get(header)); value != "" {
			return value
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil && host != "" {
		return host
	}
	if strings.TrimSpace(r.RemoteAddr) != "" {
		return strings.TrimSpace(r.RemoteAddr)
	}
	return anonymousRoutingIdentity
}

func restoreRequestBody(r *http.Request, body []byte) {
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(body)), nil
	}
}

func appendReason(reasons []string, reason string) []string {
	for _, existing := range reasons {
		if existing == reason {
			return reasons
		}
	}
	return append(reasons, reason)
}

func envString(name string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envBool(name string, fallback bool) bool {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return value
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

func sanitizeHeaderValue(value string) string {
	value = strings.ReplaceAll(value, "\r", " ")
	value = strings.ReplaceAll(value, "\n", " ")
	return strings.TrimSpace(value)
}
