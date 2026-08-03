/**
 * Accessible status badge shared by operational and execution views.
 *
 * System role: pairs semantic text/icons with status colors so state is never
 * communicated by color alone.
 * Dependencies: React and local CSS status tokens.
 * Side effects: none.
 */
import type { ReactNode } from "react";

export type StatusTone =
  | "healthy"
  | "warning"
  | "danger"
  | "info"
  | "neutral";

export interface StatusBadgeProps {
  children: ReactNode;
  tone?: StatusTone;
  pulse?: boolean;
}

export function StatusBadge({
  children,
  tone = "neutral",
  pulse = false,
}: StatusBadgeProps) {
  return (
    <span className={`status-badge status-${tone}`}>
      <span
        className={`status-dot${pulse ? " status-dot-pulse" : ""}`}
        aria-hidden="true"
      />
      {children}
    </span>
  );
}
