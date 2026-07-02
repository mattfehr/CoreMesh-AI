package autopilot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

type fakeExperimentStore struct {
	exp   Experiment
	found bool
	err   error

	mu    sync.Mutex
	calls int
}

func (s *fakeExperimentStore) LookupExperiment(context.Context, string) (Experiment, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	return s.exp, s.found, s.err
}

func TestClassifyFieldsRoutesSimpleFormattingToTier1(t *testing.T) {
	classification := ClassifyFields(parseFields(t, `{
		"model":"gpt-4o",
		"prompt":"Reformat this address as JSON with street, city, and zip fields: 1 Main St, Boston MA 02108"
	}`))

	if classification.Tier != Tier1 {
		t.Fatalf("tier = %q, want %q; reasons=%v", classification.Tier, Tier1, classification.Reasons)
	}
	if classification.CachePolicy != CachePolicyAllow {
		t.Fatalf("cache policy = %q, want %q", classification.CachePolicy, CachePolicyAllow)
	}
}

func TestClassifyFieldsRoutesComplexCodeLogicToTier3(t *testing.T) {
	classification := ClassifyFields(parseFields(t, `{
		"model":"gpt-4o-mini",
		"messages":[
			{"role":"user","content":"Analyze this Go panic and debug the concurrency root cause:\nfunc worker(ch chan int) { close(ch); ch <- 1 }\nCompare two fixes and explain the tradeoffs."}
		]
	}`))

	if classification.Tier != Tier3 {
		t.Fatalf("tier = %q, want %q; score=%d reasons=%v", classification.Tier, Tier3, classification.Score, classification.Reasons)
	}
	if classification.CachePolicy != CachePolicyBypass {
		t.Fatalf("cache policy = %q, want %q", classification.CachePolicy, CachePolicyBypass)
	}
}

func TestClassifyFieldsRoutesToolAndSchemaRequestsToTier3(t *testing.T) {
	classification := ClassifyFields(parseFields(t, `{
		"model":"gpt-4o-mini",
		"prompt":"Find the customer record and produce a validated response.",
		"tools":[{"type":"function","function":{"name":"lookup_customer"}}],
		"response_format":{"type":"json_schema","json_schema":{"name":"customer"}}
	}`))

	if classification.Tier != Tier3 {
		t.Fatalf("tier = %q, want %q; score=%d reasons=%v", classification.Tier, Tier3, classification.Score, classification.Reasons)
	}
}

func TestClassifyFieldsRoutesLongPromptsToTier3(t *testing.T) {
	words := make([]string, 0, 220)
	for i := 0; i < 220; i++ {
		words = append(words, "context")
	}
	body := fmt.Sprintf(`{"model":"gpt-4o-mini","prompt":"Summarize this material: %s"}`, strings.Join(words, " "))
	classification := ClassifyFields(parseFields(t, body))

	if classification.Tier != Tier3 {
		t.Fatalf("tier = %q, want %q; token_count=%d reasons=%v", classification.Tier, Tier3, classification.TokenCount, classification.Reasons)
	}
}

func TestMiddlewareExperimentalUsesClassifierAndRewritesModel(t *testing.T) {
	router := newTestRouter(t, &fakeExperimentStore{
		found: true,
		exp: Experiment{
			FlagName:                  defaultExperimentFlag,
			RolloutPercentage:         100,
			BaselinePromptVersion:     10,
			ExperimentalPromptVersion: 11,
			Status:                    "running",
		},
	})
	handler := router.Middleware(echoModelHandler(t))

	rec := httptest.NewRecorder()
	req := jsonRequest(`{"model":"client-picked","prompt":"Extract the invoice number from INV-123."}`)
	req.Header.Set("X-User-ID", "user-a")
	handler.ServeHTTP(rec, req)

	if got := rec.Header().Get(HeaderExperimentVariant); got != VariantExperimental {
		t.Fatalf("%s = %q, want %q", HeaderExperimentVariant, got, VariantExperimental)
	}
	if got := rec.Header().Get(HeaderAutopilotTier); got != Tier1 {
		t.Fatalf("%s = %q, want %q", HeaderAutopilotTier, got, Tier1)
	}
	if got := rec.Header().Get(HeaderRoutedModel); got != "tier-one" {
		t.Fatalf("%s = %q, want tier-one", HeaderRoutedModel, got)
	}
	if got := rec.Header().Get(HeaderPromptVersion); got != "11" {
		t.Fatalf("%s = %q, want 11", HeaderPromptVersion, got)
	}
	if rec.Body.String() != "tier-one" {
		t.Fatalf("downstream model = %q, want tier-one", rec.Body.String())
	}
}

func TestMiddlewareBaselineForcesTier3AtZeroPercent(t *testing.T) {
	router := newTestRouter(t, &fakeExperimentStore{
		found: true,
		exp: Experiment{
			RolloutPercentage:         0,
			BaselinePromptVersion:     21,
			ExperimentalPromptVersion: 22,
			Status:                    "running",
		},
	})
	handler := router.Middleware(echoModelHandler(t))

	rec := httptest.NewRecorder()
	req := jsonRequest(`{"model":"client-picked","prompt":"Extract the zip code from this address."}`)
	req.Header.Set("X-User-ID", "user-b")
	handler.ServeHTTP(rec, req)

	if got := rec.Header().Get(HeaderExperimentVariant); got != VariantBaseline {
		t.Fatalf("%s = %q, want %q", HeaderExperimentVariant, got, VariantBaseline)
	}
	if got := rec.Header().Get(HeaderAutopilotTier); got != Tier3 {
		t.Fatalf("%s = %q, want %q", HeaderAutopilotTier, got, Tier3)
	}
	if got := rec.Header().Get(HeaderPromptVersion); got != "21" {
		t.Fatalf("%s = %q, want 21", HeaderPromptVersion, got)
	}
	if got := rec.Header().Get(HeaderCachePolicy); got != CachePolicyAllow {
		t.Fatalf("%s = %q, want %q", HeaderCachePolicy, got, CachePolicyAllow)
	}
	if rec.Body.String() != "tier-three" {
		t.Fatalf("downstream model = %q, want tier-three", rec.Body.String())
	}
}

func TestMiddlewareConsistentUserHashingDoesNotDrift(t *testing.T) {
	router := newTestRouter(t, &fakeExperimentStore{
		found: true,
		exp: Experiment{
			RolloutPercentage:         50,
			BaselinePromptVersion:     31,
			ExperimentalPromptVersion: 32,
			Status:                    "running",
		},
	})
	handler := router.Middleware(echoModelHandler(t))

	var variant string
	for i := 0; i < 10; i++ {
		rec := httptest.NewRecorder()
		req := jsonRequest(`{"model":"client-picked","prompt":"Extract the customer ID."}`)
		req.Header.Set("X-User-ID", "sticky-user")
		handler.ServeHTTP(rec, req)

		got := rec.Header().Get(HeaderExperimentVariant)
		if i == 0 {
			variant = got
			continue
		}
		if got != variant {
			t.Fatalf("variant drifted from %q to %q", variant, got)
		}
	}
}

func TestMiddlewareRolledBackAndStoreErrorFailClosedToTier3(t *testing.T) {
	tests := []struct {
		name  string
		store ExperimentStore
	}{
		{
			name: "rolled back",
			store: &fakeExperimentStore{
				found: true,
				exp: Experiment{
					RolloutPercentage:     100,
					BaselinePromptVersion: 41,
					Status:                "rolled_back",
				},
			},
		},
		{
			name:  "store error",
			store: &fakeExperimentStore{err: errors.New("database unavailable")},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			router := newTestRouter(t, tt.store)
			handler := router.Middleware(echoModelHandler(t))

			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, jsonRequest(`{"model":"client-picked","prompt":"Extract the customer ID."}`))

			if got := rec.Header().Get(HeaderExperimentVariant); got != VariantBaseline {
				t.Fatalf("%s = %q, want %q", HeaderExperimentVariant, got, VariantBaseline)
			}
			if got := rec.Header().Get(HeaderAutopilotTier); got != Tier3 {
				t.Fatalf("%s = %q, want %q", HeaderAutopilotTier, got, Tier3)
			}
			if rec.Body.String() != "tier-three" {
				t.Fatalf("downstream model = %q, want tier-three", rec.Body.String())
			}
		})
	}
}

func TestMiddlewareDebugModeEmitsExperimentLookupError(t *testing.T) {
	router, err := NewRouter(Config{
		Enabled:        true,
		Tier1Model:     "tier-one",
		Tier3Model:     "tier-three",
		ExperimentFlag: defaultExperimentFlag,
		Debug:          true,
	}, &fakeExperimentStore{err: errors.New("database refused test connection\nwith newline")})
	if err != nil {
		t.Fatalf("NewRouter: %v", err)
	}
	handler := router.Middleware(echoModelHandler(t))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, jsonRequest(`{"model":"client-picked","prompt":"Extract the customer ID."}`))

	if got := rec.Header().Get(HeaderExperimentVariant); got != VariantBaseline {
		t.Fatalf("%s = %q, want %q", HeaderExperimentVariant, got, VariantBaseline)
	}
	if got := rec.Header().Get(HeaderExperimentError); got != "database refused test connection with newline" {
		t.Fatalf("%s = %q, want sanitized lookup error", HeaderExperimentError, got)
	}
}

func TestMiddlewareClassifiesWhenExperimentMissingOrCompleted(t *testing.T) {
	tests := []struct {
		name  string
		store ExperimentStore
	}{
		{name: "missing experiment", store: &fakeExperimentStore{found: false}},
		{name: "completed experiment", store: &fakeExperimentStore{
			found: true,
			exp: Experiment{
				RolloutPercentage:         0,
				BaselinePromptVersion:     51,
				ExperimentalPromptVersion: 52,
				Status:                    "completed",
			},
		}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			router := newTestRouter(t, tt.store)
			handler := router.Middleware(echoModelHandler(t))

			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, jsonRequest(`{"model":"client-picked","prompt":"Extract the customer ID."}`))

			if got := rec.Header().Get(HeaderExperimentVariant); got != VariantNone {
				t.Fatalf("%s = %q, want %q", HeaderExperimentVariant, got, VariantNone)
			}
			if got := rec.Header().Get(HeaderAutopilotTier); got != Tier1 {
				t.Fatalf("%s = %q, want %q", HeaderAutopilotTier, got, Tier1)
			}
			if rec.Body.String() != "tier-one" {
				t.Fatalf("downstream model = %q, want tier-one", rec.Body.String())
			}
		})
	}
}

func TestMiddlewarePassesMalformedJSONThroughUnchanged(t *testing.T) {
	store := &fakeExperimentStore{
		found: true,
		exp: Experiment{
			RolloutPercentage: 100,
			Status:            "running",
		},
	}
	router := newTestRouter(t, store)
	handler := router.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read body: %v", err)
		}
		fmt.Fprint(w, string(body))
	}))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, jsonRequest(`{"prompt":`))

	if rec.Header().Get(HeaderAutopilotTier) != "" {
		t.Fatalf("autopilot headers should not be set for malformed JSON")
	}
	if rec.Body.String() != `{"prompt":` {
		t.Fatalf("body = %q, want original malformed body", rec.Body.String())
	}
	if store.calls != 0 {
		t.Fatalf("experiment calls = %d, want 0", store.calls)
	}
}

func newTestRouter(t *testing.T, store ExperimentStore) *Router {
	t.Helper()
	router, err := NewRouter(Config{
		Enabled:        true,
		Tier1Model:     "tier-one",
		Tier3Model:     "tier-three",
		ExperimentFlag: defaultExperimentFlag,
	}, store)
	if err != nil {
		t.Fatalf("NewRouter: %v", err)
	}
	return router
}

func echoModelHandler(t *testing.T) http.Handler {
	t.Helper()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			Model string `json:"model"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("X-Upstream-Model", r.Header.Get(HeaderRoutedModel))
		fmt.Fprint(w, payload.Model)
	})
}

func parseFields(t *testing.T, body string) map[string]json.RawMessage {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(body), &fields); err != nil {
		t.Fatalf("unmarshal fields: %v", err)
	}
	return fields
}

func jsonRequest(body string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req
}
