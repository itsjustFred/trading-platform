export function metricValue(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function pValueMetric(value: unknown, fallback = 1): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.min(1, Math.max(0, numeric));
}

export function formatPValue(value: unknown): string {
  return pValueMetric(value).toFixed(3);
}

export function pValueColor(value: unknown): string {
  return pValueMetric(value) < 0.05 ? "#3fb950" : "#f85149";
}
