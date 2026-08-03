/**
 * Execution workspace interaction tests.
 *
 * System role: proves keyboard submission and mode selection produce a
 * restricted request through the shared gateway client.
 * Dependencies: Testing Library, TanStack Query, and execution fixtures.
 * Side effects: replaces the API execute method during the test.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { coreMeshClient } from "../api/client";
import { gatewayMetadata, sqlResult } from "../test/fixtures";
import { ExecutionPage } from "./ExecutionPage";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ExecutionPage", () => {
  it("submits the selected mode with Ctrl+Enter and safe context only", async () => {
    const execute = vi
      .spyOn(coreMeshClient, "execute")
      .mockResolvedValue({ data: sqlResult, gateway: gatewayMetadata });
    const queryClient = new QueryClient();
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <ExecutionPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "SQL" }));
    const composer = screen.getByRole("textbox", { name: "Execution prompt" });
    fireEvent.change(composer, { target: { value: "Count active features" } });
    fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });

    await waitFor(() => expect(execute).toHaveBeenCalledOnce());
    const payload = execute.mock.calls[0][0];
    expect(payload).toEqual(
      expect.objectContaining({
        user_id: "demo-user",
        feature_scope: "text_to_sql",
        payload_query: "Count active features",
        session_context: expect.objectContaining({ session_id: expect.any(String) }),
      }),
    );
    expect(payload.session_context).not.toHaveProperty("document_path");
    expect(payload.session_context).not.toHaveProperty("raw_sql");
    expect(await screen.findByText(/SELECT status, total/)).toBeInTheDocument();
  });

  it("remains usable when session storage is unavailable", () => {
    vi.spyOn(window.sessionStorage, "getItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    vi.spyOn(window.sessionStorage, "setItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    vi.spyOn(window.sessionStorage, "removeItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });

    const queryClient = new QueryClient();
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <ExecutionPage />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "Execution prompt" })).toBeEnabled();
    expect(() => fireEvent.click(screen.getByRole("button", { name: "New session" }))).not.toThrow();
  });
});
