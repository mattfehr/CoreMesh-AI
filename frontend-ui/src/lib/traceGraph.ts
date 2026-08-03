/**
 * Deterministic adapter from OpenTelemetry spans to React Flow nodes and edges.
 *
 * System role: preserves parent-child causality while assigning a stable
 * top-down layout and severity to every forensic span.
 * Dependencies: React Flow structural types and CoreMesh trace contracts.
 * Side effects: none.
 */
import type { Edge, Node } from "@xyflow/react";
import type {
  ForensicTraceArtifact,
  RootCauseDiagnosis,
  SerializedSpan,
} from "../api/types";

export type TraceSeverity = "healthy" | "warning" | "failed" | "escalated";

export interface TraceNodeData extends Record<string, unknown> {
  label: string;
  span: SerializedSpan;
  severity: TraceSeverity;
  rootCause: boolean;
}

export type TraceFlowNode = Node<TraceNodeData, "traceSpan">;

function numericAttribute(span: SerializedSpan, key: string): number | null {
  const value = span.attributes[key];
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function severityForSpan(
  span: SerializedSpan,
  diagnosis: RootCauseDiagnosis | null,
): TraceSeverity {
  if (
    span.name.includes("arbitration") &&
    (span.attributes["coremesh.arbitration.failed"] === true ||
      String(span.attributes["coremesh.arbitration.status"] ?? "") === "manual_review")
  ) {
    return "escalated";
  }
  if (span.span_id === diagnosis?.span_id || span.status.toUpperCase() === "ERROR") {
    return "failed";
  }
  const confidence = numericAttribute(span, "coremesh.quality.confidence");
  if (span.attributes["coremesh.degraded"] === true || (confidence !== null && confidence < 0.6)) {
    return "warning";
  }
  return "healthy";
}

function spanDepth(
  span: SerializedSpan,
  byID: Map<string, SerializedSpan>,
): number {
  let depth = 0;
  let parentID = span.parent_span_id;
  const visited = new Set<string>([span.span_id]);
  while (parentID && byID.has(parentID) && !visited.has(parentID)) {
    visited.add(parentID);
    depth += 1;
    parentID = byID.get(parentID)?.parent_span_id ?? null;
  }
  return depth;
}

export function toTraceFlow(artifact: ForensicTraceArtifact): {
  nodes: TraceFlowNode[];
  edges: Edge[];
} {
  const byID = new Map(artifact.spans.map((span) => [span.span_id, span]));
  const byDepth = new Map<number, SerializedSpan[]>();
  for (const span of artifact.spans) {
    const depth = spanDepth(span, byID);
    const group = byDepth.get(depth) ?? [];
    group.push(span);
    byDepth.set(depth, group);
  }

  const nodes: TraceFlowNode[] = [];
  for (const [depth, spans] of [...byDepth.entries()].sort(([a], [b]) => a - b)) {
    spans.sort(
      (left, right) =>
        new Date(left.start_time).getTime() - new Date(right.start_time).getTime() ||
        left.span_id.localeCompare(right.span_id),
    );
    const width = (spans.length - 1) * 290;
    spans.forEach((span, index) => {
      const severity = severityForSpan(span, artifact.diagnosis);
      nodes.push({
        id: span.span_id,
        type: "traceSpan",
        position: {
          x: index * 290 - width / 2,
          y: depth * 170,
        },
        data: {
          label: span.name.replace(/^coremesh\./, ""),
          span,
          severity,
          rootCause: span.span_id === artifact.diagnosis?.span_id,
        },
        draggable: false,
        selectable: true,
      });
    });
  }

  const nodeIDs = new Set(nodes.map((node) => node.id));
  const edges: Edge[] = artifact.spans
    .filter(
      (span) => span.parent_span_id && nodeIDs.has(span.parent_span_id),
    )
    .map((span) => {
      const severity = severityForSpan(span, artifact.diagnosis);
      return {
        id: `${span.parent_span_id}-${span.span_id}`,
        source: span.parent_span_id as string,
        target: span.span_id,
        type: "smoothstep",
        animated: severity === "failed" || severity === "escalated",
        className: `trace-edge trace-edge-${severity}`,
      };
    });

  return { nodes, edges };
}
