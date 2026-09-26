/**
 * Typed metric scale contract.
 *
 * Backend payloads state metric units explicitly; pages must never guess.
 * Every percent-like metric is one of:
 *  - "fraction"  0.12  → rendered as 12.00%
 *  - "points"    12.0  → rendered as 12.00%
 * `toPercentPointsForKey` is the single conversion point for keys in the table
 * below: pages pass the raw value plus the key, and never hard-code a unit.
 */

export type PercentScale = "fraction" | "points";

export const PERCENT_METRIC_SCALES = {
  // /api/backtest/{id}/stats (_stats_payload_from_result) and portfolio
  // combined_stats. Every key below is pinned to the unit the backend actually
  // emits by tests/test_metric_scale_contract.py, which parses this table and
  // probes the matching producer — change a producer and this table together.
  net_profit_pct: "points",
  win_rate: "points",
  max_drawdown_pct: "points",
  cagr: "points",
  psr: "fraction",
  total_return: "fraction",
  max_drawdown: "fraction",
} as const satisfies Record<string, PercentScale>;

export type PercentMetricKey = keyof typeof PERCENT_METRIC_SCALES;

export function metricScale(key: string): PercentScale | undefined {
  return PERCENT_METRIC_SCALES[key as PercentMetricKey];
}

/**
 * The declared scale of a key that MUST be in this table.
 *
 * Unlike `metricScale`, an undeclared key is a hard error rather than
 * `undefined`: a `percentScale={undefined}` prop silently falls back to
 * "points" in StatCard, which is how a fraction gets rendered 100x too large.
 */
export function requireMetricScale(key: PercentMetricKey): PercentScale {
  return PERCENT_METRIC_SCALES[key];
}

/** Convert a raw metric value to percent points according to its declared scale. */
export function toPercentPoints(
  value: number | null | undefined,
  scale: PercentScale,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return scale === "fraction" ? value * 100 : value;
}

/** Convert a metric to percent points using the scale this table declares. */
export function toPercentPointsForKey(
  key: PercentMetricKey,
  value: number | null | undefined,
): number | null {
  return toPercentPoints(value, PERCENT_METRIC_SCALES[key]);
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

/** Win rate for the live payloads: normalize the raw value, then to points. */
export function liveWinRateToPoints(
  value: number | null | undefined,
): number | null {
  const fraction = normalizeWinRate(value);
  return fraction == null ? null : toPercentPoints(fraction, "fraction");
}

/** Format percent points as a signed percentage string. */
export function formatPercentPoints(
  points: number | null | undefined,
  digits = 2,
): string {
  if (points == null || !Number.isFinite(points)) return "N/A";
  return `${points >= 0 ? "+" : ""}${points.toFixed(digits)}%`;
}
