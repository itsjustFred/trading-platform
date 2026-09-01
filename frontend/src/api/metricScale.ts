/**
 * Typed metric scale contract.
 *
 * Backend payloads state metric units explicitly; pages must never guess.
 * Every percent-like metric is one of:
 *  - "fraction"  0.12  → rendered as 12.00%
 *  - "points"    12.0  → rendered as 12.00%
 * `toPercentPoints` is the single conversion point: pages pass the raw value
 * plus the scale the API contract declares and render the result.
 */

export type PercentScale = "fraction" | "points";

export const PERCENT_METRIC_SCALES = {
  // /api/backtest/{id}/stats and portfolio combined_stats
  net_profit_pct: "points",
  win_rate: "points",
  max_drawdown_pct: "points",
  cagr: "points",
  psr: "fraction",
  total_return: "fraction",
  max_drawdown: "fraction",
  // canonical runner stats (pre-normalization payloads)
  total_return_raw: "fraction",
} as const satisfies Record<string, PercentScale>;

export type PercentMetricKey = keyof typeof PERCENT_METRIC_SCALES;

export function metricScale(key: string): PercentScale | undefined {
  return PERCENT_METRIC_SCALES[key as PercentMetricKey];
}

/** Convert a raw metric value to percent points according to its declared scale. */
export function toPercentPoints(
  value: number | null | undefined,
  scale: PercentScale,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return scale === "fraction" ? value * 100 : value;
}

/** Normalize a win rate that may arrive as a fraction or percent points. */
export function normalizeWinRate(
  value: number | null | undefined,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/** Format percent points as a signed percentage string. */
export function formatPercentPoints(
  points: number | null | undefined,
  digits = 2,
): string {
  if (points == null || !Number.isFinite(points)) return "N/A";
  return `${points >= 0 ? "+" : ""}${points.toFixed(digits)}%`;
}
