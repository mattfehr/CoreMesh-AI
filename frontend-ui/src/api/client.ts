/**
 * Single-origin HTTP client for every CoreMesh browser request.
 *
 * System role: guarantees execution, metrics, and traces target the Go gateway
 * rather than the private Python runtime.
 * Dependencies: Fetch API and the public contract projections in types.ts.
 * Side effects: performs gateway network requests and publishes last-run
 * headers in process memory for the observability view.
 */
import type {
  APIResult,
  ExecutionRequest,
  ForensicTraceArtifact,
  GatewayMetadata,
  ObservabilitySnapshot,
  OrchestrationResult,
  TraceFilters,
  TraceListResponse,
} from "./types";

const DEFAULT_GATEWAY_URL = "http://localhost:8080";
export const GATEWAY_METADATA_EVENT = "coremesh:gateway-metadata";
let lastGatewayMetadata: GatewayMetadata | null = null;

export class CoreMeshAPIError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly gateway: GatewayMetadata;

  constructor(
    message: string,
    status: number,
    detail: unknown,
    gateway: GatewayMetadata,
  ) {
    super(message);
    this.name = "CoreMeshAPIError";
    this.status = status;
    this.detail = detail;
    this.gateway = gateway;
  }
}

function numericHeader(headers: Headers, name: string): number | null {
  const raw = headers.get(name);
  if (raw === null || raw.trim() === "") {
    return null;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function readGatewayMetadata(headers: Headers): GatewayMetadata {
  return {
    remainingTokens: numericHeader(headers, "x-ratelimit-remaining"),
    retryAfterSeconds: numericHeader(headers, "retry-after"),
    cache: headers.get("x-coremesh-cache"),
    circuitState: headers.get("x-coremesh-circuit-state"),
    route: headers.get("x-coremesh-route"),
    autopilotTier: headers.get("x-coremesh-autopilot-tier"),
    routedModel: headers.get("x-coremesh-routed-model"),
    autopilotReason: headers.get("x-coremesh-autopilot-reason"),
  };
}

export function publishGatewayMetadata(metadata: GatewayMetadata): void {
  lastGatewayMetadata = metadata;
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(new CustomEvent(GATEWAY_METADATA_EVENT, { detail: metadata }));
}

export function getLastGatewayMetadata(): GatewayMetadata | null {
  return lastGatewayMetadata;
}

function errorMessage(status: number, payload: unknown): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      const first = detail.find(
        (item): item is { loc?: unknown; msg: string } =>
          item !== null &&
          typeof item === "object" &&
          "msg" in item &&
          typeof (item as { msg: unknown }).msg === "string",
      );
      if (first) {
        const location = Array.isArray(first.loc)
          ? first.loc
              .filter((part) => part !== "body")
              .map(String)
              .join(".")
          : "";
        return `Invalid request${location ? ` (${location})` : ""}: ${first.msg}`;
      }
    }
  }
  if (status === 429) {
    return "Gateway request budget exhausted. Retry when the token bucket refills.";
  }
  if (status === 503) {
    return "CoreMesh is temporarily unavailable.";
  }
  return `CoreMesh request failed with status ${status}.`;
}

export class CoreMeshClient {
  readonly baseURL: string;
  private readonly fetcher: typeof fetch;

  constructor(baseURL: string, fetcher: typeof fetch = fetch) {
    this.baseURL = baseURL.replace(/\/+$/, "");
    // Window.fetch performs a brand check in browsers. Binding here keeps the
    // injected test seam while preventing an "Illegal invocation" when the
    // native function is later called through this client instance.
    this.fetcher = fetcher.bind(globalThis);
  }

  private async request<T>(path: string, init?: RequestInit): Promise<APIResult<T>> {
    const response = await this.fetcher(`${this.baseURL}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        "X-Team-ID": "coremesh-frontend",
        ...init?.headers,
      },
    });
    const gateway = readGatewayMetadata(response.headers);
    if (Object.values(gateway).some((value) => value !== null)) {
      publishGatewayMetadata(gateway);
    }
    const contentType = response.headers.get("content-type") ?? "";
    const payload: unknown = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? (payload as { detail: unknown }).detail
          : payload;
      throw new CoreMeshAPIError(
        errorMessage(response.status, payload),
        response.status,
        detail,
        gateway,
      );
    }
    return { data: payload as T, gateway };
  }

  execute(payload: ExecutionRequest): Promise<APIResult<OrchestrationResult>> {
    return this.request<OrchestrationResult>("/v1/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async getObservability(): Promise<ObservabilitySnapshot> {
    return (await this.request<ObservabilitySnapshot>("/v1/observability")).data;
  }

  async listTraces(filters: TraceFilters = {}): Promise<TraceListResponse> {
    const query = new URLSearchParams();
    if (filters.limit !== undefined) query.set("limit", String(filters.limit));
    if (filters.offset !== undefined) query.set("offset", String(filters.offset));
    if (filters.status) query.set("status", filters.status);
    if (filters.trigger) query.set("trigger", filters.trigger);
    if (filters.failureCategory) {
      query.set("failure_category", filters.failureCategory);
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : "";
    return (await this.request<TraceListResponse>(`/v1/traces${suffix}`)).data;
  }

  async getTrace(traceID: string): Promise<ForensicTraceArtifact> {
    return (
      await this.request<ForensicTraceArtifact>(
        `/v1/traces/${encodeURIComponent(traceID)}`,
      )
    ).data;
  }
}

const configuredGatewayURL =
  import.meta.env.VITE_GATEWAY_BASE_URL?.trim() || DEFAULT_GATEWAY_URL;

export const coreMeshClient = new CoreMeshClient(configuredGatewayURL);
