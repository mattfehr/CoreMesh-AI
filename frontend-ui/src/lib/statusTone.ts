/**
 * Maps CoreMesh state strings to accessible presentation tones.
 *
 * System role: keeps operational semantics consistent across status badges.
 * Dependencies: shared StatusTone type.
 * Side effects: none.
 */
import type { StatusTone } from "../components/StatusBadge";

export function toneForStatus(status: string | null | undefined): StatusTone {
  const normalized = status?.toLowerCase() ?? "";
  if (
    normalized.includes("error") ||
    normalized.includes("fail") ||
    normalized.includes("blocked") ||
    normalized === "open"
  ) {
    return "danger";
  }
  if (
    normalized.includes("degraded") ||
    normalized.includes("warning") ||
    normalized.includes("half-open") ||
    normalized.includes("review")
  ) {
    return "warning";
  }
  if (
    normalized.includes("complete") ||
    normalized.includes("success") ||
    normalized.includes("pass") ||
    normalized.includes("closed") ||
    normalized === "ok"
  ) {
    return "healthy";
  }
  return normalized ? "info" : "neutral";
}
