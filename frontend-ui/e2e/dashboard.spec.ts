/**
 * Browser-level CoreMesh dashboard flows.
 *
 * System role: exercises all execution modes, metrics refresh, trace selection,
 * gateway metadata, API-origin isolation, keyboard access, and responsive CSS.
 * Dependencies: Playwright and intercepted gateway contracts.
 * Side effects: starts a local browser and Vite server; no real API is called.
 */
import { expect, test, type Page, type Route } from "@playwright/test";

const traceID = "0123456789abcdef0123456789abcdef";

const gatewayHeaders = {
  "access-control-allow-origin": "http://localhost:3000",
  "access-control-allow-headers": "content-type,x-team-id",
  "access-control-expose-headers":
    "x-ratelimit-remaining,x-coremesh-cache,x-coremesh-circuit-state,x-coremesh-route,x-coremesh-autopilot-tier,x-coremesh-routed-model",
  "content-type": "application/json",
  "x-ratelimit-remaining": "17",
  "x-coremesh-cache": "bypass",
  "x-coremesh-circuit-state": "closed",
  "x-coremesh-route": "primary",
  "x-coremesh-autopilot-tier": "standard",
  "x-coremesh-routed-model": "test-model",
};

const observability = {
  generated_at: "2026-07-28T19:00:05Z",
  started_at: "2026-07-28T19:00:00Z",
  rate_limit: { capacity: 20, refill_per_second: 2 },
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

const rootSpan = {
  trace_id: traceID,
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

const diagnosis = {
  trace_id: traceID,
  span_id: "span-sql",
  span_name: "coremesh.specialist.sql",
  step_id: "step-2",
  category: "execution_error",
  explanation: "The SQL guardrail rejected a mutating statement.",
  confidence: 0.96,
  evidence: [{ signal: "span_status", observed: "ERROR" }],
  analyzer: "deterministic",
};

const artifact = {
  schema_version: "1.0",
  trace_id: traceID,
  status: "completed_with_errors",
  trigger: "execution_error",
  trigger_reasons: ["specialist_failed"],
  started_at: "2026-07-28T19:00:00Z",
  ended_at: "2026-07-28T19:00:00.050Z",
  duration_ms: 50,
  final_confidence: 0.42,
  summary: { span_count: 2, error_count: 1 },
  diagnosis,
  spans: [
    rootSpan,
    {
      ...rootSpan,
      span_id: "span-sql",
      parent_span_id: "span-root",
      name: "coremesh.specialist.sql",
      status: "ERROR",
      duration_ms: 18,
      attributes: { "coremesh.span.category": "database" },
    },
  ],
  tree: {},
  feedback: null,
};

function orchestration(mode: string) {
  const specialist = mode === "rag" ? "rag_search" : "sql_generation";
  const output =
    mode === "rag"
      ? {
          results: [
            {
              chunk_id: "policy-1",
              reference_marker: "[policy.md:1]",
              text: "Fallback routing activates while the circuit is open.",
              rerank_score: 0.91,
            },
          ],
        }
      : {
          sql: "SELECT status, COUNT(*) AS total FROM feature_experiments GROUP BY status",
          columns: ["status", "total"],
          rows: [{ status: "active", total: 4 }],
          row_count: 1,
          elapsed_ms: 5,
          limit_applied: false,
        };
  const plan =
    mode === "agent_orchestrator"
      ? [
          {
            step_id: "step-1",
            specialist: "rag_search",
            objective: "Retrieve evidence",
            expected_output: "references",
            depends_on: [],
            complexity: "low",
            status: "completed",
          },
          {
            step_id: "step-2",
            specialist: "sql_generation",
            objective: "Validate operational data",
            expected_output: "rows",
            depends_on: ["step-1"],
            complexity: "medium",
            status: "completed",
          },
        ]
      : [
          {
            step_id: "step-1",
            specialist,
            objective: "Execute specialist task",
            expected_output: "result",
            depends_on: [],
            complexity: "low",
            status: "completed",
          },
        ];
  return {
    session_id: "browser-session",
    user_id: "demo-user",
    feature_scope: mode,
    status: "completed",
    plan,
    observations:
      mode === "agent_orchestrator"
        ? [
            {
              observation_id: "obs-rag",
              step_id: "step-1",
              specialist: "rag_search",
              status: "success",
              input_payload: {},
              output: {
                results: [
                  {
                    chunk_id: "policy-1",
                    reference_marker: "[policy.md:1]",
                    text: "Fallback routing activates while the circuit is open.",
                    rerank_score: 0.91,
                  },
                ],
              },
              error: null,
              latency_ms: 8,
              created_at_ms: 1,
            },
            {
              observation_id: "obs-sql",
              step_id: "step-2",
              specialist: "sql_generation",
              status: "success",
              input_payload: {},
              output: {
                sql: "SELECT COUNT(*) AS total FROM feature_experiments",
                columns: ["total"],
                rows: [{ total: 4 }],
                row_count: 1,
                elapsed_ms: 5,
              },
              error: null,
              latency_ms: 7,
              created_at_ms: 2,
            },
          ]
        : [
            {
              observation_id: "obs-1",
              step_id: "step-1",
              specialist,
              status: "success",
              input_payload: {},
              output,
              error: null,
              latency_ms: 9,
              created_at_ms: 1,
            },
          ],
    retrieved_memories: [],
    final_response: `${mode} browser flow completed.`,
    arbitration:
      mode === "agent_orchestrator"
        ? {
            arbitration_id: "arb-1",
            payload_id: "payload-1",
            status: "approved",
            delivery_allowed: true,
            delivered_output: "approved",
            adjudication_required: false,
            triggered_by: [],
            confidence_coefficient: 0.91,
          }
        : null,
    trace_id: traceID,
    root_cause: null,
  };
}

async function fulfillJSON(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    headers: gatewayHeaders,
    body: JSON.stringify(body),
  });
}

async function installGateway(page: Page, seen: string[]) {
  page.on("request", (request) => {
    if (["fetch", "xhr"].includes(request.resourceType())) {
      seen.push(request.url());
    }
  });
  await page.route("http://localhost:8080/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: gatewayHeaders });
      return;
    }
    const url = new URL(request.url());
    if (url.pathname === "/v1/observability") {
      await fulfillJSON(route, observability);
      return;
    }
    if (url.pathname === "/v1/traces") {
      await fulfillJSON(route, {
        items: [
          {
            trace_id: traceID,
            created_at: "2026-07-28T19:00:00Z",
            status: "completed_with_errors",
            final_confidence: 0.42,
            trigger: "execution_error",
            root_cause_span_id: "span-sql",
            root_cause_step_id: "step-2",
            failure_category: "execution_error",
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      });
      return;
    }
    if (url.pathname === `/v1/traces/${traceID}`) {
      await fulfillJSON(route, artifact);
      return;
    }
    if (url.pathname === "/v1/execute") {
      const payload = request.postDataJSON() as { feature_scope: string };
      await fulfillJSON(route, orchestration(payload.feature_scope));
      return;
    }
    await route.fulfill({ status: 404, headers: gatewayHeaders, body: "{}" });
  });
}

test("executes every mode and inspects live metrics and a trace", async ({ page }) => {
  const seen: string[] = [];
  await installGateway(page, seen);
  await page.goto("/");

  const prompt = page.getByRole("textbox", { name: "Execution prompt" });
  await prompt.fill("Find the resilience policy");
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByText("rag browser flow completed.")).toBeVisible();
  await expect(page.getByText("17 remaining")).toBeVisible();
  await expect(page.getByText("standard / test-model")).toBeVisible();

  await page.getByRole("button", { name: "SQL" }).click();
  await prompt.fill("Count active experiments");
  await prompt.press("Control+Enter");
  await expect(page.getByText("text_to_sql browser flow completed.")).toBeVisible();
  await expect(page.getByText(/SELECT status, COUNT/)).toBeVisible();

  await page.getByRole("button", { name: "Agent" }).click();
  await prompt.fill("Retrieve the policy and validate its operational data");
  await page.getByRole("button", { name: "Execute" }).click();
  await expect(page.getByText("2 planned specialist steps")).toBeVisible();

  await page.getByRole("link", { name: "Observability" }).click();
  await expect(page.getByText("40.0%")).toBeVisible();
  await expect(page.getByText("8", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Forensics" }).click();
  await expect(page.getByText("Agent execution tree")).toBeVisible();
  const sqlNode = page.getByLabel("specialist.sql, failed");
  await expect(sqlNode).toBeVisible();
  await sqlNode.click();
  await expect(page.getByText("span-sql")).toBeVisible();
  await expect(page.getByText("Diagnosis evidence")).toBeVisible();

  const root = page.locator("html");
  const initialTheme = await root.getAttribute("data-theme");
  const nextTheme = initialTheme === "dark" ? "light" : "dark";
  await expect(page.locator(".react-flow")).toHaveClass(new RegExp(initialTheme ?? "dark"));
  await page.getByRole("button", { name: `Switch to ${nextTheme} theme` }).click();
  await expect(root).toHaveAttribute("data-theme", nextTheme);
  await expect(page.locator(".react-flow")).toHaveClass(new RegExp(nextTheme));

  expect(seen.length).toBeGreaterThan(0);
  for (const requestURL of seen) {
    const url = new URL(requestURL);
    expect(url.hostname).toBe("localhost");
    expect(url.port).toBe("8080");
  }
});

test("keeps primary navigation and content within a phone viewport", async ({ page }) => {
  const seen: string[] = [];
  await installGateway(page, seen);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
});
