/**
 * Compact metric presentation for the observability control room.
 *
 * System role: standardizes labels, values, supporting text, and progress bars.
 * Dependencies: React node rendering and dashboard CSS.
 * Side effects: none.
 */
import type { ReactNode } from "react";

interface MetricCardProps {
  eyebrow: string;
  value: ReactNode;
  detail: ReactNode;
  icon: ReactNode;
  progress?: number | null;
  accent?: "cyan" | "violet" | "green" | "amber" | "red";
}

export function MetricCard({
  eyebrow,
  value,
  detail,
  icon,
  progress,
  accent = "cyan",
}: MetricCardProps) {
  const boundedProgress =
    progress === null || progress === undefined
      ? null
      : Math.max(0, Math.min(100, progress));
  return (
    <article className={`metric-card metric-${accent}`}>
      <div className="metric-card-heading">
        <span className="metric-icon" aria-hidden="true">
          {icon}
        </span>
        <span>{eyebrow}</span>
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-detail">{detail}</div>
      {boundedProgress !== null ? (
        <div
          className="metric-progress"
          role="progressbar"
          aria-label={eyebrow}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(boundedProgress)}
        >
          <span style={{ width: `${boundedProgress}%` }} />
        </div>
      ) : null}
    </article>
  );
}
