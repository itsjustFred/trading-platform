const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "paused"]);
const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "partial",
  "skipped",
  "manual_required",
  "cleanup_pending",
  "cleanup_exhausted",
]);
const FAILED_STATUSES = new Set([
  "failed",
  "interrupted",
  "manual_required",
  "cleanup_exhausted",
]);

export function normalizeTaskStatus(status: unknown): string {
  return typeof status === "string" ? status.trim().toLowerCase() : "";
}

export function isActiveTaskStatus(status: unknown): boolean {
  return ACTIVE_STATUSES.has(normalizeTaskStatus(status));
}

export function isTerminalTaskStatus(status: unknown): boolean {
  return TERMINAL_STATUSES.has(normalizeTaskStatus(status));
}

export function isFailedTaskStatus(status: unknown): boolean {
  return FAILED_STATUSES.has(normalizeTaskStatus(status));
}
