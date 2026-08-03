/**
 * Deterministic public-contract fixtures for frontend tests.
 *
 * System role: keeps API, renderer, and graph tests aligned to one redacted
 * CoreMesh execution without importing server implementation types.
 * Dependencies: frontend public contract projections.
 * Side effects: none.
 */
import type {
  ForensicTraceArtifact,
  GatewayMetadata,
  ObservabilitySnapshot,
  OrchestrationResult,
  SerializedSpan,
} from "../api/types";

export const gatewayMetadata: GatewayMetadata = {
  remainingTokens: 17,
  retryAfterSeconds: null,
  cache: "bypass",
  circuitState: "closed",
  route: "primary",
  autopilotTier: "standard",
  routedModel: "test-model",
  autopilotReason: "default",
};

const planStep = {
  step_id: "step-1",
  specialist: "rag_search" as const,
  objective: "Retrieve policy evidence",
  expected_output: "ranked evidence",
  depends_on: [],
  complexity: "low",
  status: "completed",
};

export const baseResult: OrchestrationResult = {
  session_id: "session-123456789",
  user_id: "demo-user",
  feature_scope: "rag",
  status: "completed",
  plan: [planStep],
  observations: [],
  retrieved_memories: [],
  final_response: "CoreMesh completed the requested workflow.",
  arbitration: null,
  trace_id: "0123456789abcdef0123456789abcdef",
  root_cause: null,
};

export const ragResult: OrchestrationResult = {
  ...baseResult,
  observations: [
    {
      observation_id: "observation-rag",
      step_id: "step-1",
      specialist: "rag_search",
      status: "success",
      input_payload: {},
      output: {
        results: [
          {
            chunk_id: "policy-7",
            source: "resilience.md",
            reference_marker: "[resilience.md:7]",
            text: "The circuit routes to fallback while open.",
            rerank_score: 0.93,
            dense_rank: 1,
            sparse_rank: 2,
            rrf_score: 0.041,
          },
        ],
      },
      error: null,
      latency_ms: 12,
      created_at_ms: 1_700_000_000_000,
    },
  ],
};

export const sqlResult: OrchestrationResult = {
  ...baseResult,
  feature_scope: "text_to_sql",
  plan: [
    {
      ...planStep,
      specialist: "sql_generation",
      objective: "Generate a read-only query",
    },
  ],
  observations: [
    {
      observation_id: "observation-sql",
      step_id: "step-1",
      specialist: "sql_generation",
      status: "success",
      input_payload: {},
      output: {
        sql: "SELECT status, total FROM feature_counts LIMIT 100",
        columns: ["status", "total"],
        rows: [
          { status: "active", total: 4 },
          { status: "paused", total: 2 },
        ],
        row_count: 2,
        elapsed_ms: 8,
        limit_applied: true,
      },
      error: null,
      latency_ms: 10,
      created_at_ms: 1_700_000_000_000,
    },
  ],
};

export const agentResult: OrchestrationResult = {
  ...baseResult,
  feature_scope: "agent_orchestrator",
  status: "completed_with_errors",
  plan: [
    planStep,
    {
      ...planStep,
      step_id: "step-2",
      specialist: "sql_generation",
      objective: "Validate the policy in operational data",
      depends_on: ["step-1"],
      status: "failed",
    },
  ],
  observations: [
    ...ragResult.observations,
    {
      observation_id: "observation-sql-error",
      step_id: "step-2",
      specialist: "sql_generation",
      status: "failed",
      input_payload: {},
      output: {},
      error: "read-only guardrail rejected the query",
      latency_ms: 4,
      created_at_ms: 1_700_000_000_004,
    },
  ],
  arbitration: {
    arbitration_id: "arb-1",
    payload_id: "payload-1",
    status: "manual_review",
    delivery_allowed: false,
    delivered_output: "",
    adjudication_required: true,
    triggered_by: ["execution_error"],
    confidence_coefficient: 0.42,
  },
  root_cause: {
    trace_id: "0123456789abcdef0123456789abcdef",
    span_id: "span-sql",
    span_name: "coremesh.specialist.sql",
    step_id: "step-2",
    category: "execution_error",
    explanation: "The SQL guardrail rejected a mutating statement.",
    confidence: 0.96,
    evidence: [{ signal: "span_status", observed: "ERROR" }],
    analyzer: "deterministic",
  },
};

export const observabilitySnapshot: ObservabilitySnapshot = {
  generated_at: "2026-07-28T19:00:05Z",
  started_at: "2026-07-28T19:00:00Z",
  rate_limit: {
    capacity: 20,
    refill_per_second: 2,
  },
  semantic_cache: {
    enabled: true,
    hits: 4,
    misses: 6,
    bypasses: 3,
    hit_rate: 0.4,
  },
  circuit_breaker: {
    state: "closed",
    failure_threshold: 5,
    failure_window_seconds: 30,
    open_duration_seconds: 15,
  },
  traffic: {
    requests: 13,
    primary: 8,
    fallback: 2,
    rate_limited: 1,
    upstream_errors: 2,
  },
};

const baseSpan: SerializedSpan = {
  trace_id: "0123456789abcdef0123456789abcdef",
  span_id: "span-root",
  parent_span_id: null,
  name: "coremesh.workflow",
  kind: "INTERNAL",
  status: "OK",
  start_time: "2026-07-28T19:00:00Z",
  end_time: "2026-07-28T19:00:00.050Z",
  duration_ms: 50,
  attributes: {},
  events: [],
};

export const traceArtifact: ForensicTraceArtifact = {
  schema_version: "1.0",
  trace_id: "0123456789abcdef0123456789abcdef",
  status: "completed_with_errors",
  trigger: "execution_error",
  trigger_reasons: ["specialist_failed"],
  started_at: "2026-07-28T19:00:00Z",
  ended_at: "2026-07-28T19:00:00.050Z",
  duration_ms: 50,
  final_confidence: 0.42,
  summary: { span_count: 3, error_count: 1, degraded_count: 1 },
  diagnosis: agentResult.root_cause,
  spans: [
    baseSpan,
    {
      ...baseSpan,
      span_id: "span-rag",
      parent_span_id: "span-root",
      name: "coremesh.specialist.rag",
      duration_ms: 20,
      attributes: { "coremesh.quality.confidence": 0.45 },
    },
    {
      ...baseSpan,
      span_id: "span-sql",
      parent_span_id: "span-root",
      name: "coremesh.specialist.sql",
      status: "ERROR",
      duration_ms: 18,
      attributes: { "coremesh.span.category": "database" },
      events: [{ name: "exception", attributes: { redacted: true } }],
    },
  ],
  tree: {},
  feedback: null,
};
