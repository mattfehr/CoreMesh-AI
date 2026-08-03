/**
 * Mode-aware renderer for unified orchestration results.
 *
 * System role: turns specialist observations into SQL tables, RAG evidence,
 * agent timelines, arbitration diagnostics, and gateway routing metadata.
 * Dependencies: React state, Router trace links, and public API contracts.
 * Side effects: table sorting is local browser state only.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowUpDown,
  Braces,
  Clock3,
  Cpu,
  Database,
  ExternalLink,
  FileSearch,
  GitBranch,
  Gauge,
  Route,
  ShieldCheck,
  Sparkles,
  TimerReset,
} from "lucide-react";
import type {
  GatewayMetadata,
  OrchestrationResult,
  ToolObservation,
} from "../api/types";
import { compactID, formatDuration, formatPercent, formatValue } from "../lib/format";
import { toneForStatus } from "../lib/statusTone";
import { StatusBadge } from "./StatusBadge";

interface ExecutionResultProps {
  result: OrchestrationResult;
  gateway: GatewayMetadata;
}

function records(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          item !== null && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

function findObservation(
  result: OrchestrationResult,
  specialist: ToolObservation["specialist"],
): ToolObservation | undefined {
  return result.observations.find(
    (observation) =>
      observation.specialist === specialist && observation.status === "success",
  );
}

function GatewayRunStrip({ gateway }: { gateway: GatewayMetadata }) {
  const entries = [
    {
      label: "Budget",
      value:
        gateway.remainingTokens === null
          ? "Not reported"
          : `${gateway.remainingTokens} remaining`,
      icon: <Gauge size={14} />,
    },
    {
      label: "Cache",
      value: gateway.cache ?? "not enabled",
      icon: <Sparkles size={14} />,
    },
    {
      label: "Circuit",
      value: gateway.circuitState ?? "not reported",
      icon: <ShieldCheck size={14} />,
    },
    {
      label: "Route",
      value: gateway.route ?? "cache/local",
      icon: <Route size={14} />,
    },
    {
      label: "Model tier",
      value:
        gateway.autopilotTier && gateway.routedModel
          ? `${gateway.autopilotTier} / ${gateway.routedModel}`
          : gateway.autopilotTier ?? gateway.routedModel ?? "autopilot unavailable",
      icon: <Cpu size={14} />,
    },
    {
      label: "Retry timing",
      value:
        gateway.retryAfterSeconds === null
          ? "no retry delay"
          : `${gateway.retryAfterSeconds}s`,
      icon: <TimerReset size={14} />,
    },
  ];
  return (
    <div className="run-metadata" aria-label="Gateway response metadata">
      {entries.map((entry) => (
        <div key={entry.label}>
          <span aria-hidden="true">{entry.icon}</span>
          <span>
            <small>{entry.label}</small>
            <strong>{entry.value}</strong>
          </span>
        </div>
      ))}
    </div>
  );
}

function SortableResultTable({
  columns,
  rows,
}: {
  columns: string[];
  rows: Array<Record<string, unknown>>;
}) {
  const [sort, setSort] = useState<{ column: string; direction: 1 | -1 } | null>(
    null,
  );
  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    return [...rows].sort((left, right) => {
      const a = left[sort.column];
      const b = right[sort.column];
      if (typeof a === "number" && typeof b === "number") {
        return (a - b) * sort.direction;
      }
      return String(a ?? "").localeCompare(String(b ?? "")) * sort.direction;
    });
  }, [rows, sort]);

  if (columns.length === 0) {
    return <div className="empty-inline">The query returned no columns.</div>;
  }
  return (
    <div className="data-table-shell">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>
                <button
                  type="button"
                  onClick={() =>
                    setSort((current) => ({
                      column,
                      direction:
                        current?.column === column && current.direction === 1 ? -1 : 1,
                    }))
                  }
                >
                  {column}
                  <ArrowUpDown size={13} aria-hidden="true" />
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column) => (
                <td key={column}>{formatValue(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SQLResult({ observation }: { observation: ToolObservation }) {
  const sql = String(observation.output.sql ?? "");
  const columns = Array.isArray(observation.output.columns)
    ? observation.output.columns.map(String)
    : [];
  const rows = records(observation.output.rows);
  return (
    <section className="result-section">
      <div className="result-section-heading">
        <div>
          <span className="section-icon">
            <Database size={17} />
          </span>
          <div>
            <p className="eyebrow">Guardrailed query</p>
            <h4>Text-to-SQL result</h4>
          </div>
        </div>
        <div className="result-stats">
          <span>{String(observation.output.row_count ?? rows.length)} rows</span>
          <span>{formatDuration(Number(observation.output.elapsed_ms ?? 0))}</span>
          {observation.output.limit_applied ? <span>Limit applied</span> : null}
        </div>
      </div>
      <pre className="sql-block">
        <code>{sql || "No SQL was produced."}</code>
      </pre>
      <SortableResultTable columns={columns} rows={rows} />
    </section>
  );
}

function RAGResult({ observation }: { observation: ToolObservation }) {
  const hits = records(observation.output.results);
  return (
    <section className="result-section">
      <div className="result-section-heading">
        <div>
          <span className="section-icon">
            <FileSearch size={17} />
          </span>
          <div>
            <p className="eyebrow">Hybrid retrieval</p>
            <h4>{hits.length} ranked references</h4>
          </div>
        </div>
      </div>
      {hits.length === 0 ? (
        <div className="empty-inline">
          No matching evidence was found in the configured RAG corpus.
        </div>
      ) : (
        <div className="evidence-list">
          {hits.map((hit, index) => (
            <article
              className="evidence-card"
              key={String(hit.chunk_id ?? index)}
            >
              <div className="evidence-heading">
                <strong>
                  {String(
                    hit.reference_marker ??
                      `[${hit.source ?? "source"}:${hit.chunk_id ?? index + 1}]`,
                  )}
                </strong>
                <span>
                  score {Number(hit.rerank_score ?? hit.score ?? 0).toFixed(3)}
                </span>
              </div>
              <p>{String(hit.text ?? "No evidence text returned.")}</p>
              <div className="evidence-ranks">
                <span>Dense #{String(hit.dense_rank ?? "—")}</span>
                <span>Sparse #{String(hit.sparse_rank ?? "—")}</span>
                <span>RRF {Number(hit.rrf_score ?? 0).toFixed(4)}</span>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function AgentPlan({ result }: { result: OrchestrationResult }) {
  return (
    <section className="result-section">
      <div className="result-section-heading">
        <div>
          <span className="section-icon">
            <GitBranch size={17} />
          </span>
          <div>
            <p className="eyebrow">Supervisor workflow</p>
            <h4>{result.plan.length} planned specialist steps</h4>
          </div>
        </div>
      </div>
      <ol className="plan-timeline">
        {result.plan.map((step, index) => {
          const observation = result.observations.find(
            (item) => item.step_id === step.step_id,
          );
          return (
            <li key={step.step_id}>
              <span className="timeline-index">{index + 1}</span>
              <div>
                <div className="timeline-heading">
                  <strong>{step.specialist.replaceAll("_", " ")}</strong>
                  <StatusBadge tone={toneForStatus(observation?.status ?? step.status)}>
                    {observation?.status ?? step.status}
                  </StatusBadge>
                </div>
                <p>{step.objective}</p>
                {observation ? (
                  <span className="timeline-latency">
                    <Clock3 size={13} aria-hidden="true" />
                    {formatDuration(observation.latency_ms)}
                    {observation.error ? ` · ${observation.error}` : ""}
                  </span>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function ExecutionResult({ result, gateway }: ExecutionResultProps) {
  const sqlObservation = findObservation(result, "sql_generation");
  const ragObservation = findObservation(result, "rag_search");
  return (
    <div className="execution-result">
      <div className="execution-answer">
        <div className="answer-heading">
          <div>
            <Braces size={16} aria-hidden="true" />
            <span>CoreMesh synthesis</span>
          </div>
          <StatusBadge tone={toneForStatus(result.status)}>
            {result.status.replaceAll("_", " ")}
          </StatusBadge>
        </div>
        <p>{result.final_response || "The workflow completed without a deliverable."}</p>
      </div>

      {result.feature_scope === "agent_orchestrator" || result.plan.length > 1 ? (
        <AgentPlan result={result} />
      ) : null}
      {ragObservation ? <RAGResult observation={ragObservation} /> : null}
      {sqlObservation ? <SQLResult observation={sqlObservation} /> : null}

      {result.root_cause ? (
        <aside className="root-cause-callout">
          <ShieldCheck size={18} aria-hidden="true" />
          <div>
            <strong>Forensic diagnosis: {result.root_cause.category}</strong>
            <p>{result.root_cause.explanation}</p>
            <span>
              Confidence {formatPercent(result.root_cause.confidence)} ·{" "}
              {result.root_cause.span_name}
            </span>
          </div>
        </aside>
      ) : null}

      <div className="result-footer">
        <div>
          <span>Session {compactID(result.session_id)}</span>
          {result.arbitration ? (
            <span>
              Arbitration {result.arbitration.status} ·{" "}
              {formatPercent(result.arbitration.confidence_coefficient)}
            </span>
          ) : null}
        </div>
        {result.trace_id ? (
          <Link to={`/forensics?trace=${result.trace_id}`} className="trace-link">
            Inspect trace {compactID(result.trace_id, 4)}
            <ExternalLink size={13} aria-hidden="true" />
          </Link>
        ) : null}
      </div>
      <GatewayRunStrip gateway={gateway} />
    </div>
  );
}
