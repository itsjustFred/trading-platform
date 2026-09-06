/**
 * Chart-time helpers shared by the live viewer's marker builders.
 *
 * Extracted from pages/LiveViewer.tsx so the marker timing rules stay
 * testable without a DOM (matches tests/statFormat.test.ts).
 */

/** Unix seconds → lightweight-charts UTCTimestamp, or null when unusable. */
export function toChartTime(value: number): number | null {
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }
  return Math.trunc(value);
}

/**
 * Parses an ISO timestamp string into Unix seconds, or null when unparseable.
 * Bare "Z"-less UTC strings are treated as UTC (see LiveViewer normalization).
 */
export function parseIsoSeconds(value: string): number | null {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return Math.trunc(parsed / 1000);
}

/**
 * Chart time for a live trade exit: the execution timestamp when the row was
 * filled, otherwise the signal timestamp as a fallback. Returns null when
 * neither source yields a usable time.
 */
export function resolveExitChartTime(
  executionTime: number,
  signalTime: number,
): number | null {
  return toChartTime(executionTime) ?? toChartTime(signalTime);
}

/**
 * Stable per-parameter element id prefix for OptimizeModal rows so labels
 * never collide across parameters (a11y: htmlFor → id must be unique).
 */
export function parameterFieldId(name: string): string {
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return `optimize-param-${slug.replace(/^-+/, "").replace(/-+$/, "") || "unnamed"}`;
}
