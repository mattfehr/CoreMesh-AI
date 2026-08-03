/**
 * Unified chat and execution workspace.
 *
 * System role: submits restricted RAG, text-to-SQL, and agent requests through
 * the Go gateway and presents specialist-specific evidence.
 * Dependencies: TanStack mutations, sessionStorage, and ExecutionResult.
 * Side effects: sends POST /v1/execute and persists display-only session history.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Bot,
  Database,
  FileSearch,
  LoaderCircle,
  Network,
  Plus,
  Send,
  Sparkles,
  Trash2,
  UserRound,
} from "lucide-react";
import {
  CoreMeshAPIError,
  coreMeshClient,
} from "../api/client";
import type {
  FeatureScope,
  GatewayMetadata,
  OrchestrationResult,
} from "../api/types";
import { ExecutionResult } from "../components/ExecutionResult";

interface ConversationMessage {
  id: string;
  role: "user" | "assistant" | "error";
  text: string;
  mode: FeatureScope;
  createdAt: string;
  result?: OrchestrationResult;
  gateway?: GatewayMetadata;
}

const SESSION_ID_KEY = "coremesh:execution-session-id";
const HISTORY_KEY = "coremesh:execution-history";

const modeOptions: Array<{
  value: FeatureScope;
  label: string;
  shortLabel: string;
  description: string;
  icon: typeof FileSearch;
  examples: string[];
}> = [
  {
    value: "rag",
    label: "RAG Retrieval",
    shortLabel: "RAG",
    description: "Search the configured hybrid dense/sparse knowledge corpus.",
    icon: FileSearch,
    examples: [
      "Find the policy for circuit breaker recovery.",
      "Search for CircuitBreakerState.OPEN routing behavior.",
    ],
  },
  {
    value: "text_to_sql",
    label: "Text-to-SQL",
    shortLabel: "SQL",
    description: "Generate and execute a guardrailed read-only database query.",
    icon: Database,
    examples: [
      "Count the active feature experiments in the database.",
      "Analyze prompt registry records by status.",
    ],
  },
  {
    value: "agent_orchestrator",
    label: "Agent Orchestrator",
    shortLabel: "Agent",
    description: "Let the supervisor plan and dispatch multiple specialist steps.",
    icon: Network,
    examples: [
      "Search the policy references, then analyze the database count.",
      "Find relevant knowledge and explain how it affects SQL operations.",
    ],
  },
];

function newID(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function readSessionValue(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSessionValue(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Display state is optional when browser storage is unavailable or full.
  }
}

function removeSessionValue(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Display state is optional when browser storage is unavailable.
  }
}

function loadSessionID(): string {
  const existing = readSessionValue(SESSION_ID_KEY);
  if (existing) return existing;
  const created = newID();
  writeSessionValue(SESSION_ID_KEY, created);
  return created;
}

function loadHistory(): ConversationMessage[] {
  const raw = readSessionValue(HISTORY_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as ConversationMessage[]) : [];
  } catch {
    return [];
  }
}

function modeLabel(mode: FeatureScope): string {
  return modeOptions.find((item) => item.value === mode)?.shortLabel ?? mode;
}

export function ExecutionPage() {
  const [mode, setMode] = useState<FeatureScope>("rag");
  const [userID, setUserID] = useState("demo-user");
  const [query, setQuery] = useState("");
  const [ragTopK, setRagTopK] = useState(5);
  const [sessionID, setSessionID] = useState(loadSessionID);
  const [messages, setMessages] = useState<ConversationMessage[]>(loadHistory);
  const conversationEnd = useRef<HTMLDivElement>(null);

  const mutation = useMutation({
    mutationFn: coreMeshClient.execute.bind(coreMeshClient),
  });

  const activeMode = useMemo(
    () => modeOptions.find((item) => item.value === mode) ?? modeOptions[0],
    [mode],
  );
  const ActiveModeIcon = activeMode.icon;

  useEffect(() => {
    writeSessionValue(HISTORY_KEY, JSON.stringify(messages.slice(-20)));
    conversationEnd.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages]);

  function append(message: ConversationMessage) {
    setMessages((current) => [...current, message].slice(-20));
  }

  function startNewSession() {
    const created = newID();
    writeSessionValue(SESSION_ID_KEY, created);
    removeSessionValue(HISTORY_KEY);
    setSessionID(created);
    setMessages([]);
    mutation.reset();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const normalizedQuery = query.trim();
    const normalizedUser = userID.trim();
    if (!normalizedQuery || !normalizedUser || mutation.isPending) return;

    const submittedMode = mode;
    append({
      id: newID(),
      role: "user",
      text: normalizedQuery,
      mode: submittedMode,
      createdAt: new Date().toISOString(),
    });
    setQuery("");
    mutation.mutate(
      {
        user_id: normalizedUser,
        feature_scope: submittedMode,
        payload_query: normalizedQuery,
        session_context: {
          session_id: sessionID,
          ...(submittedMode === "rag" ? { rag_top_k: ragTopK } : {}),
        },
      },
      {
        onSuccess: ({ data, gateway }) => {
          append({
            id: newID(),
            role: "assistant",
            text: data.final_response,
            mode: submittedMode,
            createdAt: new Date().toISOString(),
            result: data,
            gateway,
          });
        },
        onError: (error) => {
          const apiError = error instanceof CoreMeshAPIError ? error : null;
          const retry =
            apiError?.gateway.retryAfterSeconds !== null &&
            apiError?.gateway.retryAfterSeconds !== undefined
              ? ` Retry after ${apiError.gateway.retryAfterSeconds} seconds.`
              : "";
          append({
            id: newID(),
            role: "error",
            text: `${error instanceof Error ? error.message : "Execution failed."}${retry}`,
            mode: submittedMode,
            createdAt: new Date().toISOString(),
            gateway: apiError?.gateway,
          });
        },
      },
    );
  }

  return (
    <div className="execution-layout">
      <section className="workspace-card execution-workspace">
        <div className="mode-switcher" aria-label="Execution mode">
          {modeOptions.map((option) => {
            const Icon = option.icon;
            return (
              <button
                type="button"
                key={option.value}
                className={mode === option.value ? "active" : ""}
                aria-pressed={mode === option.value}
                onClick={() => setMode(option.value)}
              >
                <Icon size={16} aria-hidden="true" />
                <span>{option.shortLabel}</span>
              </button>
            );
          })}
        </div>

        <div className="conversation" aria-live="polite">
          {messages.length === 0 ? (
            <div className="conversation-empty">
              <span className="empty-orbit" aria-hidden="true">
                <Sparkles size={28} />
              </span>
              <p className="eyebrow">Gateway-routed intelligence</p>
              <h2>What should CoreMesh execute?</h2>
              <p>
                Choose a specialist mode, or hand a multi-stage task to the
                supervisor. Every run receives gateway admission and resilience
                metadata.
              </p>
              <div className="prompt-suggestions">
                {activeMode.examples.map((example) => (
                  <button type="button" key={example} onClick={() => setQuery(example)}>
                    {example}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((message) => (
              <article
                className={`conversation-message message-${message.role}`}
                key={message.id}
              >
                <div className="message-avatar" aria-hidden="true">
                  {message.role === "user" ? (
                    <UserRound size={16} />
                  ) : (
                    <Bot size={16} />
                  )}
                </div>
                <div className="message-body">
                  <div className="message-meta">
                    <strong>
                      {message.role === "user"
                        ? "You"
                        : message.role === "error"
                          ? "Execution error"
                          : "CoreMesh"}
                    </strong>
                    <span>{modeLabel(message.mode)}</span>
                    <time>{new Date(message.createdAt).toLocaleTimeString()}</time>
                  </div>
                  {message.result && message.gateway ? (
                    <ExecutionResult
                      result={message.result}
                      gateway={message.gateway}
                    />
                  ) : (
                    <p className="message-text">{message.text}</p>
                  )}
                </div>
              </article>
            ))
          )}
          {mutation.isPending ? (
            <div className="execution-pending">
              <LoaderCircle size={17} className="spin" aria-hidden="true" />
              <span>CoreMesh is planning and executing specialist work…</span>
            </div>
          ) : null}
          <div ref={conversationEnd} />
        </div>

        <form className="prompt-composer" onSubmit={submit}>
          <textarea
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder={`Ask ${activeMode.label}…`}
            aria-label="Execution prompt"
            rows={3}
            maxLength={16_384}
          />
          <div className="composer-footer">
            <span>
              Ctrl/⌘ + Enter to run · {query.length.toLocaleString()} / 16,384
            </span>
            <button
              className="primary-button"
              type="submit"
              disabled={!query.trim() || !userID.trim() || mutation.isPending}
            >
              {mutation.isPending ? (
                <LoaderCircle size={16} className="spin" />
              ) : (
                <Send size={16} />
              )}
              Execute
            </button>
          </div>
        </form>
      </section>

      <aside className="execution-sidebar">
        <section className="workspace-card run-configuration">
          <div className="card-heading">
            <div>
              <p className="eyebrow">Active route</p>
              <h3>{activeMode.label}</h3>
            </div>
            <ActiveModeIcon size={19} aria-hidden="true" />
          </div>
          <p>{activeMode.description}</p>
          <label>
            Demo user ID
            <input
              value={userID}
              onChange={(event) => setUserID(event.target.value)}
              maxLength={128}
            />
          </label>
          {mode === "rag" ? (
            <label>
              Result count
              <input
                type="number"
                value={ragTopK}
                min={1}
                max={20}
                onChange={(event) =>
                  setRagTopK(Math.max(1, Math.min(20, Number(event.target.value))))
                }
              />
            </label>
          ) : null}
          <div className="session-identity">
            <span>Session</span>
            <code>{sessionID}</code>
          </div>
          <button className="secondary-button" type="button" onClick={startNewSession}>
            <Plus size={15} />
            New session
          </button>
        </section>

        <section className="workspace-card posture-card">
          <p className="eyebrow">Request posture</p>
          <ul>
            <li>
              <span className="posture-dot posture-green" />
              Gateway admission and circuit routing
            </li>
            <li>
              <span className="posture-dot posture-cyan" />
              Execution cache bypass enforced
            </li>
            <li>
              <span className="posture-dot posture-violet" />
              Redacted OpenTelemetry trace emitted
            </li>
          </ul>
        </section>

        {messages.length > 0 ? (
          <button
            className="clear-history"
            type="button"
            onClick={() => setMessages([])}
          >
            <Trash2 size={14} />
            Clear display history
          </button>
        ) : null}
      </aside>
    </div>
  );
}
