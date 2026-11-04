export function metricValue(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function pValueMetric(value: unknown, fallback = 1): number {
  // `Number(null)` is 0, so a plain finiteness check would turn an unavailable
  // p-value into the best attainable one. Keep the absence explicit.
  if (!hasMeasuredPValue(value)) {
    return fallback;
  }
  return Math.min(1, Math.max(0, Number(value)));
}

function hasMeasuredPValue(value: unknown): boolean {
  if (value == null || value === "") return false;
  return Number.isFinite(Number(value));
}

export function formatPValue(value: unknown): string {
  return hasMeasuredPValue(value) ? pValueMetric(value).toFixed(3) : "N/A";
}

export function pValueColor(value: unknown): string {
  if (!hasMeasuredPValue(value)) return "var(--color-text-muted)";
  return pValueMetric(value) < 0.05 ? "#3fb950" : "#f85149";
}
