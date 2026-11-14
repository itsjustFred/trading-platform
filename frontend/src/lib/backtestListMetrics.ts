/**
 * Filter, bounds and sort rules for the backtest list.
 *
 * A metric that was not measured arrives as `null` (see PR #114: the API
 * publishes `null`, the cards render "—"). Every helper here keeps that `null`
 * as-is. Nothing in this module coerces an unmeasured value to `0` (or to any
 * other stand-in), because a real zero and "not measured" are different facts.
 */

// Explicit `.ts` so `node --test` can load this module directly.
import { normalizeWinRate, toPercentPointsForKey } from "../api/metricScale.ts";
import type { BacktestMetadataItem } from "../api/types.ts";

export interface NumericRange {
  min: number;
  max: number;
}

/** The numeric metrics the list can filter on. Values are in display units. */
export interface BacktestListMetrics {
  totalReturn: number | null;
  sharpe: number | null;
  psr: number | null;
  pValue: number | null;
  cagr: number | null;
  maxDrawdown: number | null;
  winRate: number | null;
}

export type NumericFilters = Record<keyof BacktestListMetrics, NumericRange>;

/** Metric fields that sort by their raw (or normalized win-rate) value. */
export type SortableMetricField =
  | "total_return"
  | "sharpe_ratio"
  | "psr"
  | "p_value"
  | "cagr"
  | "max_drawdown"
  | "win_rate"
  | "trade_count";

const LIST_METRIC_KEYS: ReadonlyArray<keyof BacktestListMetrics> = [
  "totalReturn",
  "sharpe",
  "psr",
  "pValue",
  "cagr",
  "maxDrawdown",
  "winRate",
];

/** A finite number, or `null` when the metric was not measured. */
export function measuredOrNull(
  value: number | null | undefined,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return value;
}

/** Magnitude of a drawdown, or `null` when it was not measured. */
function absMeasured(value: number | null | undefined): number | null {
  const measured = measuredOrNull(value);
  return measured === null ? null : Math.abs(measured);
}

/** The filterable metrics of one backtest, in the units the sliders use. */
export function listMetricsFor(item: BacktestMetadataItem): BacktestListMetrics {
  return {
    totalReturn: toPercentPointsForKey(
      "backtest_list",
      "total_return",
      measuredOrNull(item.total_return),
    ),
    sharpe: measuredOrNull(item.sharpe_ratio),
    psr: measuredOrNull(item.psr),
    pValue: measuredOrNull(item.p_value),
    cagr: toPercentPointsForKey(
      "backtest_list",
      "cagr",
      measuredOrNull(item.cagr),
    ),
    maxDrawdown: toPercentPointsForKey(
      "backtest_list",
      "max_drawdown",
      absMeasured(item.max_drawdown),
    ),
    winRate: toPercentPointsForKey(
      "backtest_list",
      "win_rate",
      measuredOrNull(item.win_rate),
    ),
  };
}

/**
 * Slider bounds spanning the measured values. Unmeasured values are skipped;
 * when nothing was measured the fallback range is returned.
 */
export function boundsFromValues(
  values: ReadonlyArray<number | null | undefined>,
  fallback: NumericRange,
): NumericRange {
  const measured = values
    .map((value) => measuredOrNull(value))
    .filter((value): value is number => value !== null);
  if (measured.length === 0) return { ...fallback };
  return {
    min: Math.min(...measured),
    max: Math.max(...measured),
  };
}

/**
 * True only when the value is measured and lies inside the range. An
 * unmeasured value is excluded: it cannot satisfy a bound.
 */
export function inNumericRange(
  value: number | null,
  range: NumericRange,
): boolean {
  return value !== null && value >= range.min && value <= range.max;
}

/** True when a range has been narrowed away from its default full bounds. */
export function isNarrowedRange(range: NumericRange, bounds: NumericRange): boolean {
  return range.min !== bounds.min || range.max !== bounds.max;
}

/**
 * A metric filter excludes an unmeasured row ONLY once the user has narrowed
 * that metric's range away from its default bounds. With default sliders
 * nothing is hidden: every row with a null metric stays visible. Measured
 * values always lie inside the default bounds, so they pass either way.
 */
export function passesNumericFilters(
  metrics: BacktestListMetrics,
  filters: NumericFilters,
  bounds: NumericFilters,
): boolean {
  return LIST_METRIC_KEYS.every((key) => {
    if (!isNarrowedRange(filters[key], bounds[key])) return true;
    return inNumericRange(metrics[key], filters[key]);
  });
}

/** The value a list row sorts by, or `null` when it was not measured. */
export function sortableMetric(
  item: BacktestMetadataItem,
  field: SortableMetricField,
): number | null {
  if (field === "win_rate") return normalizeWinRate(item.win_rate);
  return measuredOrNull(item[field]);
}

/**
 * Comparator for one metric. Unmeasured values sort after every measured
 * value in BOTH directions; they are never compared as zero. `direction` is
 * `1` for ascending and `-1` for descending.
 */
export function compareMeasuredLast(
  left: number | null,
  right: number | null,
  direction: 1 | -1,
): number {
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return (left - right) * direction;
}
