/**
 * Browser-side projections of CoreMesh's public HTTP contracts.
 *
 * System role: gives execution, observability, and forensic views one typed
 * boundary without importing runtime implementation details.
 * Dependencies: mirrors FastAPI and Go JSON field names.
 * Side effects: none.
 */

export type FeatureScope = "rag" | "text_to_sql" | "agent_orchestrator";
export type SpecialistName =
  | "rag_search"
  | "document_extraction"
  | "sql_generation";

export interface ExecutionRequest {
  user_id: string;
  feature_scope: FeatureScope;
  payload_query: string;
  session_context?: {
    session_id?: string;
    rag_top_k?: number;
  };
}

export interface PlanStep {
  step_id: string;
  specialist: SpecialistName;
  objective: string;
  expected_output: string;
  depends_on: string[];
  complexity: string;
  status: string;
}

export interface ToolObservation {
  observation_id: string;
  step_id: string;
  specialist: SpecialistName;
  status: string;
  input_payload: Record<string, unknown>;
  output: Record<string, unknown>;
  error: string | null;
  latency_ms: number;
  created_at_ms: number;
}

export interface TraceEvidence {
  signal: string;
  observed?: string | number | boolean | null;
  threshold?: string | number | boolean | null;
}

export interface RootCauseDiagnosis {
  trace_id: string;
  span_id: string;
  span_name: string;
  step_id: string | null;
  category: string;
  explanation: string;
  confidence: number;
  evidence: TraceEvidence[];
  analyzer: string;
}

export interface ArbitrationVerdict {
  arbitration_id: string;
  payload_id: string;
  status: string;
  delivery_allowed: boolean;
  delivered_output: string;
  adjudication_required: boolean;
  triggered_by: string[];
  confidence_coefficient: number;
  critic_assessments?: Array<{
    evaluation_dimension: string;
    assigned_score: number;
    flagged_anomalies: string[];
    confidence_coefficient: number;
  }>;
  [key: string]: unknown;
}

export interface OrchestrationResult {
  session_id: string;
  user_id: string;
  feature_scope: string;
  status: string;
  plan: PlanStep[];
  observations: ToolObservation[];
  retrieved_memories: Array<Record<string, unknown>>;
  final_response: string;
  arbitration: ArbitrationVerdict | null;
  trace_id: string | null;
  root_cause: RootCauseDiagnosis | null;
}

export interface GatewayMetadata {
  remainingTokens: number | null;
  retryAfterSeconds: number | null;
  cache: string | null;
  circuitState: string | null;
  route: string | null;
  autopilotTier: string | null;
  routedModel: string | null;
  autopilotReason: string | null;
}

export interface APIResult<T> {
  data: T;
  gateway: GatewayMetadata;
}

export interface ObservabilitySnapshot {
  generated_at: string;
  started_at: string;
  rate_limit: {
    capacity: number;
    refill_per_second: number;
  };
  semantic_cache: {
    enabled: boolean;
    hits: number;
    misses: number;
    bypasses: number;
    hit_rate: number | null;
  };
  circuit_breaker: {
    state: string;
    failure_threshold: number;
    failure_window_seconds: number;
    open_duration_seconds: number;
  };
  traffic: {
    requests: number;
    primary: number;
    fallback: number;
    rate_limited: number;
    upstream_errors: number;
  };
}

export interface TraceSummary {
  trace_id: string;
  created_at: string;
  status: string;
  final_confidence: number | null;
  trigger: string | null;
  root_cause_span_id: string | null;
  root_cause_step_id: string | null;
  failure_category: string | null;
}

export interface TraceListResponse {
  items: TraceSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface SerializedSpan {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  status: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  attributes: Record<string, unknown>;
  events: Array<Record<string, unknown>>;
}

export interface ForensicTraceArtifact {
  schema_version: string;
  trace_id: string;
  status: string;
  trigger: string | null;
  trigger_reasons: string[];
  started_at: string;
  ended_at: string;
  duration_ms: number;
  final_confidence: number | null;
  summary: {
    span_count?: number;
    error_count?: number;
    degraded_count?: number;
    [key: string]: unknown;
  };
  diagnosis: RootCauseDiagnosis | null;
  spans: SerializedSpan[];
  tree: Record<string, unknown> | Array<Record<string, unknown>>;
  feedback: Record<string, unknown> | null;
}

export interface TraceFilters {
  limit?: number;
  offset?: number;
  status?: string;
  trigger?: string;
  failureCategory?: string;
}
