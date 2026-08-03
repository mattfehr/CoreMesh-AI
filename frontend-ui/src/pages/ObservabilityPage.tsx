/**
 * Live Go gateway observability workspace.
 *
 * System role: polls the local gateway snapshot and combines it with the last
 * browser-visible response budget to explain admission and resilience state.
 * Dependencies: TanStack Query, gateway metadata events, and metric cards.
 * Side effects: sends GET /v1/observability every five seconds.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertOctagon,
  Clock3,
  Gauge,
  HardDrive,
  RefreshCw,
  Route,
  ServerCrash,
  ShieldCheck,
  Sparkles,
  TimerReset,
  Zap,
} from "lucide-react";
import {
  GATEWAY_METADATA_EVENT,
  coreMeshClient,
  getLastGatewayMetadata,
} from "../api/client";
import type { GatewayMetadata } from "../api/types";
import { MetricCard } from "../components/MetricCard";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime, formatPercent } from "../lib/format";
import { toneForStatus } from "../lib/statusTone";

function useLastGatewayMetadata() {
  const [metadata, setMetadata] = useState<GatewayMetadata | null>(
    getLastGatewayMetadata,
  );
  useEffect(() => {
    const update = (event: Event) => {
      const detail = (event as CustomEvent<GatewayMetadata>).detail;
      setMetadata(detail ?? getLastGatewayMetadata());
    };
    window.addEventListener(GATEWAY_METADATA_EVENT, update);
    return () => window.removeEventListener(GATEWAY_METADATA_EVENT, update);
  }, []);
  return metadata;
}

export function ObservabilityPage() {
  const metadata = useLastGatewayMetadata();
  const query = useQuery({
    queryKey: ["observability"],
    queryFn: () => coreMeshClient.getObservability(),
    refetchInterval: 5_000,
    retry: 1,
  });

  const data = query.data;
  const budgetProgress = useMemo(() => {
    if (!data || metadata?.remainingTokens === null || metadata?.remainingTokens === undefined) {
      return null;
    }
    return (metadata.remainingTokens / data.rate_limit.capacity) * 100;
  }, [data, metadata]);

  if (query.isPending) {
    return (
      <div className="full-state">
        <RefreshCw size={24} className="spin" />
        <h2>Connecting to gateway :8080</h2>
        <p>Waiting for the first operational snapshot.</p>
      </div>
    );
  }

  if (query.isError || !data) {
    return (
      <div className="full-state full-state-error">
        <ServerCrash size={28} />
        <h2>Gateway observability is unavailable</h2>
        <p>
          Confirm the Go gateway is running on port 8080 and the frontend origin
          is in <code>GATEWAY_ALLOWED_ORIGINS</code>.
        </p>
        <button className="primary-button" type="button" onClick={() => query.refetch()}>
          <RefreshCw size={15} />
          Retry connection
        </button>
      </div>
    );
  }

  const routed = data.traffic.primary + data.traffic.fallback;
  const primaryShare = routed > 0 ? (data.traffic.primary / routed) * 100 : 0;
  const fallbackShare = routed > 0 ? (data.traffic.fallback / routed) * 100 : 0;
  const cacheRate = data.semantic_cache.hit_rate;

  return (
    <div className="observability-page">
      <div className="live-strip">
        <div>
          <StatusBadge
            tone={toneForStatus(data.circuit_breaker.state)}
            pulse={data.circuit_breaker.state === "half-open"}
          >
            circuit {data.circuit_breaker.state}
          </StatusBadge>
          <span>
            Polling every 5 seconds · counters since{" "}
            {formatDateTime(data.started_at)}
          </span>
        </div>
        <button
          className="secondary-button compact-button"
          type="button"
          onClick={() => query.refetch()}
          disabled={query.isFetching}
        >
          <RefreshCw size={14} className={query.isFetching ? "spin" : ""} />
          Refresh
        </button>
      </div>

      <section className="metric-grid" aria-label="Gateway metrics">
        <MetricCard
          eyebrow="Admission tokens"
          value={
            metadata?.remainingTokens === null || metadata?.remainingTokens === undefined
              ? "—"
              : `${metadata.remainingTokens} / ${data.rate_limit.capacity}`
          }
          detail={
            metadata?.remainingTokens === null || metadata?.remainingTokens === undefined
              ? "Run an execution to observe this browser's remaining budget."
              : `${data.rate_limit.refill_per_second.toLocaleString()} tokens refill each second`
          }
          icon={<Gauge size={18} />}
          progress={budgetProgress}
          accent="cyan"
        />
        <MetricCard
          eyebrow="Semantic cache hit rate"
          value={data.semantic_cache.enabled ? formatPercent(cacheRate) : "Disabled"}
          detail={
            data.semantic_cache.enabled
              ? `${data.semantic_cache.hits} hits · ${data.semantic_cache.misses} misses · ${data.semantic_cache.bypasses} bypasses`
              : `${data.semantic_cache.bypasses} requests bypassed cache`
          }
          icon={<Sparkles size={18} />}
          progress={cacheRate === null ? null : cacheRate * 100}
          accent={data.semantic_cache.enabled ? "violet" : "amber"}
        />
        <MetricCard
          eyebrow="Circuit breaker"
          value={data.circuit_breaker.state}
          detail={`${data.circuit_breaker.failure_threshold} failures / ${data.circuit_breaker.failure_window_seconds}s · opens ${data.circuit_breaker.open_duration_seconds}s`}
          icon={<ShieldCheck size={18} />}
          accent={
            data.circuit_breaker.state === "closed"
              ? "green"
              : data.circuit_breaker.state === "open"
                ? "red"
                : "amber"
          }
        />
        <MetricCard
          eyebrow="Gateway traffic"
          value={data.traffic.requests.toLocaleString()}
          detail={`${data.traffic.rate_limited} rate-limited · ${data.traffic.upstream_errors} gateway errors`}
          icon={<Activity size={18} />}
          accent={data.traffic.upstream_errors > 0 ? "amber" : "green"}
        />
      </section>

      <div className="observability-grid">
        <section className="workspace-card route-panel">
          <div className="card-heading">
            <div>
              <p className="eyebrow">Resilience routing</p>
              <h3>Primary versus fallback</h3>
            </div>
            <Route size={19} aria-hidden="true" />
          </div>
          <div className="route-total">
            <strong>{routed.toLocaleString()}</strong>
            <span>upstream-routed requests</span>
          </div>
          <div className="route-bars">
            <div>
              <div>
                <span>Primary</span>
                <strong>{data.traffic.primary.toLocaleString()}</strong>
              </div>
              <div className="bar-track">
                <span className="bar-primary" style={{ width: `${primaryShare}%` }} />
              </div>
              <small>{primaryShare.toFixed(1)}%</small>
            </div>
            <div>
              <div>
                <span>Fallback</span>
                <strong>{data.traffic.fallback.toLocaleString()}</strong>
              </div>
              <div className="bar-track">
                <span className="bar-fallback" style={{ width: `${fallbackShare}%` }} />
              </div>
              <small>{fallbackShare.toFixed(1)}%</small>
            </div>
          </div>
          <div className="operational-note">
            <Zap size={16} aria-hidden="true" />
            <p>
              Cache hits and requests rejected before upstream selection do not
              appear in this route split.
            </p>
          </div>
        </section>

        <section className="workspace-card operations-ledger">
          <div className="card-heading">
            <div>
              <p className="eyebrow">Operational ledger</p>
              <h3>Current gateway posture</h3>
            </div>
            <HardDrive size={19} aria-hidden="true" />
          </div>
          <dl>
            <div>
              <dt>
                <Clock3 size={15} />
                Snapshot generated
              </dt>
              <dd>{formatDateTime(data.generated_at)}</dd>
            </div>
            <div>
              <dt>
                <TimerReset size={15} />
                Browser retry delay
              </dt>
              <dd>
                {metadata?.retryAfterSeconds === null ||
                metadata?.retryAfterSeconds === undefined
                  ? "None"
                  : `${metadata.retryAfterSeconds}s`}
              </dd>
            </div>
            <div>
              <dt>
                <AlertOctagon size={15} />
                Gateway 5xx responses
              </dt>
              <dd>{data.traffic.upstream_errors.toLocaleString()}</dd>
            </div>
            <div>
              <dt>
                <Sparkles size={15} />
                Last cache disposition
              </dt>
              <dd>{metadata?.cache ?? "No browser request yet"}</dd>
            </div>
            <div>
              <dt>
                <Route size={15} />
                Last upstream route
              </dt>
              <dd>{metadata?.route ?? "No browser request yet"}</dd>
            </div>
          </dl>
        </section>
      </div>
    </div>
  );
}
