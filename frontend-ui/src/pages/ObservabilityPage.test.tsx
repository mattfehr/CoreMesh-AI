/**
 * Gateway observability state tests.
 *
 * System role: covers semantic-cache disabled/empty data and stable rendering
 * of operational counters returned by the local gateway endpoint.
 * Dependencies: Testing Library, TanStack Query, and client mocks.
 * Side effects: replaces one API method for each isolated test.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { coreMeshClient } from "../api/client";
import { observabilitySnapshot } from "../test/fixtures";
import { ObservabilityPage } from "./ObservabilityPage";

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ObservabilityPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ObservabilityPage", () => {
  it("labels a disabled semantic cache and empty routing split", async () => {
    vi.spyOn(coreMeshClient, "getObservability").mockResolvedValue({
      ...observabilitySnapshot,
      semantic_cache: {
        enabled: false,
        hits: 0,
        misses: 0,
        bypasses: 7,
        hit_rate: null,
      },
      traffic: {
        requests: 7,
        primary: 0,
        fallback: 0,
        rate_limited: 0,
        upstream_errors: 0,
      },
    });

    renderPage();

    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("7 requests bypassed cache")).toBeInTheDocument();
    expect(screen.getByText("upstream-routed requests")).toBeInTheDocument();
    expect(screen.getAllByText("0.0%")).toHaveLength(2);
  });
});
