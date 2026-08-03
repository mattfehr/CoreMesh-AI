/**
 * Forensic graph adapter tests.
 *
 * System role: validates causal edges, deterministic depth layout, and
 * severity/root-cause projection before React Flow renders the artifact.
 * Dependencies: Vitest, trace adapter, and a redacted trace fixture.
 * Side effects: none.
 */
import { describe, expect, it } from "vitest";
import { severityForSpan, toTraceFlow } from "./traceGraph";
import { traceArtifact } from "../test/fixtures";

describe("trace graph conversion", () => {
  it("creates top-down parent-child edges", () => {
    const graph = toTraceFlow(traceArtifact);
    const root = graph.nodes.find((node) => node.id === "span-root");
    const rag = graph.nodes.find((node) => node.id === "span-rag");

    expect(graph.nodes).toHaveLength(3);
    expect(graph.edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ source: "span-root", target: "span-rag" }),
        expect.objectContaining({ source: "span-root", target: "span-sql" }),
      ]),
    );
    expect(root?.position.y).toBe(0);
    expect(rag?.position.y).toBeGreaterThan(root?.position.y ?? 0);
  });

  it("marks diagnosed, degraded, and healthy spans distinctly", () => {
    const [root, rag, sql] = traceArtifact.spans;

    expect(severityForSpan(root, traceArtifact.diagnosis)).toBe("healthy");
    expect(severityForSpan(rag, traceArtifact.diagnosis)).toBe("warning");
    expect(severityForSpan(sql, traceArtifact.diagnosis)).toBe("failed");

    const sqlNode = toTraceFlow(traceArtifact).nodes.find(
      (node) => node.id === "span-sql",
    );
    expect(sqlNode?.data.rootCause).toBe(true);
    expect(sqlNode?.data.severity).toBe("failed");
  });

  it("keeps a diagnosed manual_review arbitration root cause escalated", () => {
    const arbitrationSpan = {
      ...traceArtifact.spans[0],
      span_id: "span-arbitration",
      parent_span_id: "span-root",
      name: "coremesh.tool.arbitration",
      status: "OK",
      attributes: { "coremesh.arbitration.status": "manual_review" },
    };
    const arbitrationDiagnosis = traceArtifact.diagnosis
      ? {
          ...traceArtifact.diagnosis,
          span_id: arbitrationSpan.span_id,
          span_name: arbitrationSpan.name,
          category: "arbitration_failure" as const,
        }
      : null;

    expect(severityForSpan(arbitrationSpan, arbitrationDiagnosis)).toBe(
      "escalated",
    );
  });
});
