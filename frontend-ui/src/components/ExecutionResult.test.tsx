/**
 * Specialist result rendering tests.
 *
 * System role: verifies SQL, RAG, and multi-agent outputs remain legible and
 * retain forensic navigation and diagnosed failure context.
 * Dependencies: Testing Library, MemoryRouter, and contract fixtures.
 * Side effects: renders isolated DOM trees only.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ExecutionResult } from "./ExecutionResult";
import {
  agentResult,
  gatewayMetadata,
  ragResult,
  sqlResult,
} from "../test/fixtures";

function renderResult(result: typeof ragResult) {
  return render(
    <MemoryRouter>
      <ExecutionResult result={result} gateway={gatewayMetadata} />
    </MemoryRouter>,
  );
}

describe("ExecutionResult", () => {
  it("renders ranked RAG evidence and gateway metadata", () => {
    renderResult(ragResult);

    expect(screen.getByText("[resilience.md:7]")).toBeInTheDocument();
    expect(
      screen.getByText("The circuit routes to fallback while open."),
    ).toBeInTheDocument();
    expect(screen.getByText("17 remaining")).toBeInTheDocument();
    expect(screen.getByText("standard / test-model")).toBeInTheDocument();
    expect(screen.getByText("no retry delay")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /inspect trace/i })).toHaveAttribute(
      "href",
      "/forensics?trace=0123456789abcdef0123456789abcdef",
    );
  });

  it("renders and sorts a guarded SQL result table", () => {
    renderResult(sqlResult);

    expect(screen.getByText(/SELECT status, total/)).toBeInTheDocument();
    expect(screen.getByText("Limit applied")).toBeInTheDocument();
    const statusHeader = screen.getByRole("button", { name: /status/i });
    fireEvent.click(statusHeader);
    const rows = screen.getAllByRole("row");
    expect(rows[1]).toHaveTextContent("active");
    fireEvent.click(statusHeader);
    expect(screen.getAllByRole("row")[1]).toHaveTextContent("paused");
  });

  it("renders agent steps, arbitration, and root-cause evidence", () => {
    renderResult(agentResult);

    expect(screen.getByText("2 planned specialist steps")).toBeInTheDocument();
    expect(screen.getByText(/Forensic diagnosis: execution_error/i)).toBeInTheDocument();
    expect(screen.getByText(/SQL guardrail rejected/i)).toBeInTheDocument();
    expect(screen.getByText(/Arbitration manual_review/i)).toBeInTheDocument();
  });
});
