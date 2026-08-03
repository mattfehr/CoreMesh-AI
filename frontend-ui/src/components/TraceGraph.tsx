/**
 * Interactive forensic trace-tree canvas.
 *
 * System role: visualizes redacted OpenTelemetry causality and returns the
 * selected span to the detail inspector.
 * Dependencies: React Flow, Lucide icons, and the deterministic trace adapter.
 * Side effects: viewport interactions remain local to the browser.
 */
import { memo, useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  Database,
  GitBranch,
  Search,
  ShieldAlert,
  Wrench,
} from "lucide-react";
import type {
  ForensicTraceArtifact,
  SerializedSpan,
} from "../api/types";
import { formatDuration } from "../lib/format";
import { useAppTheme } from "../lib/theme";
import {
  toTraceFlow,
  type TraceFlowNode,
  type TraceNodeData,
  type TraceSeverity,
} from "../lib/traceGraph";

function iconForSpan(data: TraceNodeData) {
  const category = String(data.span.attributes["coremesh.span.category"] ?? "");
  if (category === "database") return <Database size={15} />;
  if (category === "tool") return <Wrench size={15} />;
  if (category === "agent") return <Bot size={15} />;
  if (data.span.name.includes("rag")) return <Search size={15} />;
  if (data.span.name.includes("workflow")) return <GitBranch size={15} />;
  if (data.severity === "failed") return <ShieldAlert size={15} />;
  if (data.severity === "warning") return <AlertTriangle size={15} />;
  return <CheckCircle2 size={15} />;
}

const TraceSpanNode = memo(function TraceSpanNode({
  data,
  selected,
}: NodeProps<TraceFlowNode>) {
  return (
    <div
      className={`trace-node trace-node-${data.severity}${selected ? " trace-node-selected" : ""}`}
      aria-label={`${data.label}, ${data.severity}`}
    >
      <Handle type="target" position={Position.Top} className="trace-handle" />
      <div className="trace-node-heading">
        <span aria-hidden="true">{iconForSpan(data)}</span>
        <strong>{data.label}</strong>
      </div>
      <div className="trace-node-meta">
        <span>{formatDuration(data.span.duration_ms)}</span>
        <span>{data.span.status}</span>
      </div>
      {data.rootCause ? <span className="root-cause-label">Root cause</span> : null}
      <Handle type="source" position={Position.Bottom} className="trace-handle" />
    </div>
  );
});

const nodeTypes: NodeTypes = { traceSpan: TraceSpanNode };

const severityColors: Record<TraceSeverity, string> = {
  healthy: "#35d399",
  warning: "#f6c85f",
  failed: "#ff627d",
  escalated: "#a78bfa",
};

interface TraceGraphProps {
  artifact: ForensicTraceArtifact;
  onSelectSpan: (span: SerializedSpan) => void;
}

export function TraceGraph({ artifact, onSelectSpan }: TraceGraphProps) {
  const flow = useMemo(() => toTraceFlow(artifact), [artifact]);
  const theme = useAppTheme();

  return (
    <div className="trace-canvas" data-testid="trace-canvas">
      <ReactFlow
        nodes={flow.nodes}
        edges={flow.edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.22 }}
        minZoom={0.22}
        maxZoom={1.8}
        nodesConnectable={false}
        nodesDraggable={false}
        onNodeClick={(_, node) => onSelectSpan(node.data.span)}
        proOptions={{ hideAttribution: false }}
        colorMode={theme}
      >
        <Background color="rgba(115, 154, 190, 0.14)" gap={24} size={1} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) =>
            severityColors[(node.data?.severity as TraceSeverity) ?? "healthy"]
          }
          maskColor="rgba(4, 12, 22, 0.76)"
        />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
