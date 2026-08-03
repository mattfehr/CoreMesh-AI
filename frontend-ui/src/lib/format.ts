/**
 * Pure formatting helpers for operational data.
 *
 * System role: keeps dates, durations, percentages, and JSON values consistent
 * across the three dashboard views.
 * Dependencies: browser Intl implementations.
 * Side effects: none.
 */

export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatDuration(milliseconds: number | null | undefined): string {
  if (milliseconds === null || milliseconds === undefined) {
    return "—";
  }
  if (milliseconds < 1_000) {
    return `${milliseconds.toFixed(milliseconds < 10 ? 1 : 0)} ms`;
  }
  return `${(milliseconds / 1_000).toFixed(2)} s`;
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  return `${(value * 100).toFixed(1)}%`;
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "—";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return JSON.stringify(value);
}

export function compactID(value: string | null | undefined, edge = 6): string {
  if (!value) return "—";
  if (value.length <= edge * 2 + 1) return value;
  return `${value.slice(0, edge)}…${value.slice(-edge)}`;
}
