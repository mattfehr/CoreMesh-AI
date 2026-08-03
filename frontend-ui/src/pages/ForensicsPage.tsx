/**
 * Read-only OpenTelemetry forensic explorer.
 *
 * System role: queries redacted trace summaries through the gateway, renders
 * causal span trees, and inspects diagnosis evidence without exposing paths.
 * Dependencies: TanStack Query, React Flow TraceGraph, and Router search params.
 * Side effects: sends paginated GET requests to trace endpoints.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Clock3,
  FileWarning,
  Fingerprint,
  Layers3,
  LockKeyhole,
  RefreshCw,
  Search,
  ShieldAlert,
} from "lucide-react";
import { coreMeshClient } from "../api/client";
import type { SerializedSpan, TraceFilters } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { TraceGraph } from "../components/TraceGraph";
import {
  compactID,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatValue,
} from "../lib/format";
import { toneForStatus } from "../lib/statusTone";

const PAGE_SIZE = 20;

function AttributeInspector({ span }: { span: SerializedSpan | null }) {
  if (!span) {
    return (
      <div className="inspector-empty">
        <Search size={23} />
        <p>Select a span node to inspect redacted execution metadata.</p>
      </div>
    );
  }
  const attributes = Object.entries(span.attributes);
  return (
    <div className="span-inspector-content">
      <div className="inspector-heading">
        <div>
          <p className="eyebrow">Selected span</p>
          <h3>{span.name.replace(/^coremesh\./, "")}</h3>
        </div>
        <StatusBadge tone={toneForStatus(span.status)}>{span.status}</StatusBadge>
      </div>
      <dl className="span-basics">
        <div>
          <dt>Span ID</dt>
          <dd><code>{span.span_id}</code></dd>
        </div>
        <div>
          <dt>Parent</dt>
          <dd><code>{span.parent_span_id ?? "root"}</code></dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{formatDuration(span.duration_ms)}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{formatDateTime(span.start_time)}</dd>
        </div>
      </dl>
      <div className="attribute-list">
        <h4>Attributes</h4>
        {attributes.length === 0 ? (
          <p>No exported attributes.</p>
        ) : (
          attributes.map(([key, value]) => (
            <div key={key}>
              <code>{key}</code>
              <span>{formatValue(value)}</span>
            </div>
          ))
        )}
      </div>
      {span.events.length > 0 ? (
        <div className="event-list">
          <h4>Events</h4>
          {span.events.map((event, index) => (
            <pre key={index}>{JSON.stringify(event, null, 2)}</pre>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function ForensicsPage() {
  const [searchParams] = useSearchParams();
  const linkedTraceID = searchParams.get("trace");
  const [chosenTraceID, setChosenTraceID] = useState<string | null>(linkedTraceID);
  const [selectedSpanID, setSelectedSpanID] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [triggerFilter, setTriggerFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");

  const filters = useMemo<TraceFilters>(
    () => ({
      limit: PAGE_SIZE,
      offset,
      status: statusFilter || undefined,
      trigger: triggerFilter || undefined,
      failureCategory: categoryFilter || undefined,
    }),
    [categoryFilter, offset, statusFilter, triggerFilter],
  );

  const listQuery = useQuery({
    queryKey: ["traces", filters],
    queryFn: () => coreMeshClient.listTraces(filters),
    refetchInterval: 10_000,
  });

  const selectedTraceID =
    chosenTraceID ?? linkedTraceID ?? listQuery.data?.items[0]?.trace_id ?? null;
  const detailQuery = useQuery({
    queryKey: ["trace", selectedTraceID],
    queryFn: () => coreMeshClient.getTrace(selectedTraceID as string),
    enabled: Boolean(selectedTraceID),
  });
  const artifact = detailQuery.data;
  const selectedSpan =
    artifact?.spans.find((span) => span.span_id === selectedSpanID) ??
    artifact?.spans[0] ??
    null;

  const hasNext =
    listQuery.data !== undefined &&
    offset + PAGE_SIZE < listQuery.data.total;

  return (
    <div className="forensics-page">
      <section className="workspace-card trace-index">
        <div className="trace-index-heading">
          <div>
            <p className="eyebrow">Trace registry</p>
            <h2>Recent executions</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="Refresh traces"
            onClick={() => listQuery.refetch()}
          >
            <RefreshCw size={16} className={listQuery.isFetching ? "spin" : ""} />
          </button>
        </div>
        <div className="trace-filters">
          <label>
            Status
            <select
              value={statusFilter}
              onChange={(event) => {
                setStatusFilter(event.target.value);
                setOffset(0);
              }}
            >
              <option value="">All</option>
              <option value="completed">Completed</option>
              <option value="completed_with_errors">With errors</option>
              <option value="remediated_by_arbitration">Remediated</option>
              <option value="blocked_by_arbitration">Blocked</option>
            </select>
          </label>
          <label>
            Trigger
            <select
              value={triggerFilter}
              onChange={(event) => {
                setTriggerFilter(event.target.value);
                setOffset(0);
              }}
            >
              <option value="">All</option>
              <option value="execution_error">Execution error</option>
              <option value="arbitration_failure">Arbitration failure</option>
              <option value="negative_feedback">Negative feedback</option>
            </select>
          </label>
          <label>
            Root cause
            <select
              value={categoryFilter}
              onChange={(event) => {
                setCategoryFilter(event.target.value);
                setOffset(0);
              }}
            >
              <option value="">All</option>
              <option value="execution_error">Execution error</option>
              <option value="extraction_degradation">Extraction degradation</option>
              <option value="low_confidence">Low confidence</option>
              <option value="propagation_error">Propagation</option>
              <option value="arbitration_failure">Arbitration</option>
              <option value="prompt_failure">Prompt failure</option>
              <option value="context_loss">Context loss</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
        </div>

        <div className="trace-list">
          {listQuery.isPending ? (
            <div className="list-state">
              <RefreshCw size={18} className="spin" />
              Loading registry…
            </div>
          ) : listQuery.isError ? (
            <div className="list-state list-state-error">
              <FileWarning size={18} />
              Trace registry unavailable
            </div>
          ) : listQuery.data?.items.length === 0 ? (
            <div className="list-state">
              <Layers3 size={20} />
              No traces match these filters.
            </div>
          ) : (
            listQuery.data?.items.map((trace) => (
              <button
                type="button"
                className={selectedTraceID === trace.trace_id ? "active" : ""}
                key={trace.trace_id}
                onClick={() => {
                  setChosenTraceID(trace.trace_id);
                  setSelectedSpanID(null);
                }}
              >
                <span className="trace-list-topline">
                  <code>{compactID(trace.trace_id, 5)}</code>
                  <StatusBadge tone={toneForStatus(trace.status)}>
                    {trace.status.replaceAll("_", " ")}
                  </StatusBadge>
                </span>
                <span className="trace-list-time">
                  {formatDateTime(trace.created_at)}
                </span>
                <span className="trace-list-diagnosis">
                  {trace.failure_category ?? trace.trigger ?? "No diagnosed failure"}
                  {trace.final_confidence !== null
                    ? ` · ${formatPercent(trace.final_confidence)}`
                    : ""}
                </span>
              </button>
            ))
          )}
        </div>
        <div className="pagination">
          <button
            type="button"
            aria-label="Previous trace page"
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
            disabled={offset === 0}
          >
            <ChevronLeft size={15} />
          </button>
          <span>
            {listQuery.data?.total
              ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, listQuery.data.total)} of ${listQuery.data.total}`
              : "0 traces"}
          </span>
          <button
            type="button"
            aria-label="Next trace page"
            onClick={() => setOffset((value) => value + PAGE_SIZE)}
            disabled={!hasNext}
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </section>

      <section className="trace-workspace">
        {!selectedTraceID ? (
          <div className="full-state workspace-card">
            <Fingerprint size={27} />
            <h2>No trace selected</h2>
            <p>Run a task in Execution Studio to create a forensic tree.</p>
          </div>
        ) : detailQuery.isPending ? (
          <div className="full-state workspace-card">
            <RefreshCw size={22} className="spin" />
            <h2>Loading trace {compactID(selectedTraceID)}</h2>
          </div>
        ) : detailQuery.isError || !artifact ? (
          <div className="full-state full-state-error workspace-card">
            <ShieldAlert size={27} />
            <h2>Trace artifact unavailable</h2>
            <p>The registry entry may outlive a removed or disabled artifact.</p>
          </div>
        ) : (
          <>
            <section className="workspace-card trace-overview">
              <div className="trace-overview-heading">
                <div>
                  <p className="eyebrow">Trace {compactID(artifact.trace_id, 8)}</p>
                  <h2>Agent execution tree</h2>
                </div>
                <StatusBadge tone={toneForStatus(artifact.status)}>
                  {artifact.status.replaceAll("_", " ")}
                </StatusBadge>
              </div>
              <div className="trace-kpis">
                <div>
                  <Clock3 size={15} />
                  <span>
                    <strong>{formatDuration(artifact.duration_ms)}</strong>
                    wall-clock
                  </span>
                </div>
                <div>
                  <Layers3 size={15} />
                  <span>
                    <strong>{String(artifact.summary.span_count ?? artifact.spans.length)}</strong>
                    spans
                  </span>
                </div>
                <div>
                  <AlertTriangle size={15} />
                  <span>
                    <strong>{String(artifact.summary.error_count ?? 0)}</strong>
                    errors
                  </span>
                </div>
                <div>
                  <LockKeyhole size={15} />
                  <span>
                    <strong>Redacted</strong>
                    content boundary
                  </span>
                </div>
              </div>
              {artifact.diagnosis ? (
                <div className="diagnosis-banner">
                  <ShieldAlert size={17} />
                  <div>
                    <strong>
                      {artifact.diagnosis.category.replaceAll("_", " ")} at{" "}
                      {artifact.diagnosis.span_name}
                    </strong>
                    <p>{artifact.diagnosis.explanation}</p>
                  </div>
                  <span>{formatPercent(artifact.diagnosis.confidence)}</span>
                </div>
              ) : null}
            </section>
            <section className="workspace-card graph-panel">
              <TraceGraph
                artifact={artifact}
                onSelectSpan={(span) => setSelectedSpanID(span.span_id)}
              />
            </section>
          </>
        )}
      </section>

      <aside className="workspace-card span-inspector">
        <AttributeInspector span={selectedSpan} />
        {artifact?.diagnosis?.evidence.length ? (
          <div className="diagnosis-evidence">
            <h4>Diagnosis evidence</h4>
            {artifact.diagnosis.evidence.map((evidence, index) => (
              <div key={`${evidence.signal}-${index}`}>
                <strong>{evidence.signal.replaceAll("_", " ")}</strong>
                <span>
                  {formatValue(evidence.observed)}
                  {evidence.threshold !== null && evidence.threshold !== undefined
                    ? ` / threshold ${formatValue(evidence.threshold)}`
                    : ""}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </aside>
    </div>
  );
}
