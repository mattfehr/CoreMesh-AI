/**
 * Gateway client boundary tests.
 *
 * System role: proves every browser request targets the configured Go gateway
 * and that structured/429 failures preserve response metadata.
 * Dependencies: Vitest, Fetch API response primitives, and CoreMeshClient.
 * Side effects: none outside JSDOM session storage.
 */
import { describe, expect, it, vi } from "vitest";
import { CoreMeshAPIError, CoreMeshClient } from "./client";
import { baseResult } from "../test/fixtures";

function jsonResponse(
  payload: unknown,
  init: { status?: number; headers?: Record<string, string> } = {},
): Response {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    headers: {
      "content-type": "application/json",
      ...init.headers,
    },
  });
}

describe("CoreMeshClient", () => {
  it("routes execution through the configured gateway and reads headers", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(baseResult, {
        headers: {
          "x-ratelimit-remaining": "17",
          "x-coremesh-cache": "bypass",
          "x-coremesh-circuit-state": "closed",
          "x-coremesh-route": "primary",
        },
      }),
    );
    const client = new CoreMeshClient("http://localhost:8080/", fetcher);

    const response = await client.execute({
      user_id: "demo-user",
      feature_scope: "rag",
      payload_query: "Find the fallback policy",
      session_context: { session_id: "session-1", rag_top_k: 5 },
    });

    expect(fetcher).toHaveBeenCalledOnce();
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("http://localhost:8080/v1/execute");
    expect(String(url)).not.toContain(":8000");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toMatchObject({ "X-Team-ID": "coremesh-frontend" });
    expect(response.gateway).toMatchObject({
      remainingTokens: 17,
      cache: "bypass",
      circuitState: "closed",
      route: "primary",
    });
  });

  it("surfaces rate limits with retry metadata", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        {},
        {
          status: 429,
          headers: {
            "retry-after": "3",
            "x-ratelimit-remaining": "0",
          },
        },
      ),
    );
    const client = new CoreMeshClient("http://localhost:8080", fetcher);

    const error = await client.getObservability().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(CoreMeshAPIError);
    expect(error).toMatchObject({
      status: 429,
      message:
        "Gateway request budget exhausted. Retry when the token bucket refills.",
      gateway: {
        remainingTokens: 0,
        retryAfterSeconds: 3,
      },
    });
  });

  it("turns FastAPI validation details into a safe field message", async () => {
    const detail = [
      {
        type: "less_than_equal",
        loc: ["body", "session_context", "rag_top_k"],
        msg: "Input should be less than or equal to 20",
        input: 99,
      },
    ];
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ detail }, { status: 422 }));
    const client = new CoreMeshClient("http://localhost:8080", fetcher);

    const error = await client
      .execute({
        user_id: "demo-user",
        feature_scope: "rag",
        payload_query: "Find policy",
        session_context: { rag_top_k: 99 },
      })
      .catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(CoreMeshAPIError);
    expect(error).toMatchObject({
      status: 422,
      message:
        "Invalid request (session_context.rag_top_k): Input should be less than or equal to 20",
      detail,
    });
  });

  it("encodes trace filters without exposing runtime paths", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ items: [], total: 0, limit: 10, offset: 20 }),
    );
    const client = new CoreMeshClient("http://localhost:8080", fetcher);

    await client.listTraces({
      limit: 10,
      offset: 20,
      status: "completed_with_errors",
      trigger: "execution_error",
      failureCategory: "context_loss",
    });

    const url = String(fetcher.mock.calls[0][0]);
    expect(url).toContain("http://localhost:8080/v1/traces?");
    expect(url).toContain("failure_category=context_loss");
    expect(url).not.toContain("artifact_path");
    expect(url).not.toContain(":8000");
  });
});
