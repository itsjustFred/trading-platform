/**
 * Typed, per-payload metric scale contract.
 *
 * Backend payloads state metric units explicitly; pages must never guess.
 * Every percent-like metric is one of:
 *  - "fraction"  0.12  → rendered as 12.00%
 *  - "points"    12.0  → rendered as 12.00%
 *
 * The unit is a property of the PAYLOAD, not of the key: `win_rate` is
 * percent points in `/api/backtest/{id}/stats` (the frontend multiplies the
 * engine's fraction by 100 before serving) and a fraction in
 * `/api/backtests/all` (served straight from the engine's canonical stats).
 * A single flat table cannot express both, and binding these three keys to one
 * unit would render them 100x wrong, so the table is keyed by payload and
 * every accessor requires the caller to name the payload it is reading.
 *
 * Every payload declares EVERY key, either with its unit or as
 * "not-applicable" when that payload does not emit the key at all. The
 * `satisfies` clause makes a missing key a compile error, so a new key cannot
 * slip past a payload that never declared it, and
 * `tests/test_metric_scale_contract.py` parses this file and probes the
 * matching backend producer for every non-"not-applicable" cell.
 */

export type PercentScale = "fraction" | "points";

/** A declared cell: the unit, or an explicit "this payload has no such key". */
export type PercentScaleEntry = PercentScale | "not-applicable";

/**
 * Every percent-scaled key, in one list, so each payload table below is
 * checked against the same key set.
 */
export const PERCENT_METRIC_KEYS = [
  "net_profit_pct",
  "win_rate",
  "max_drawdown_pct",
  "max_drawdown",
  "cagr",
  "psr",
  "total_return",
] as const;

export type PercentMetricKey = (typeof PERCENT_METRIC_KEYS)[number];

/**
 * The payloads that serve those keys. Named for the endpoint that produces
 * them; the producer is named in each table below and pinned by the contract
 * test.
 */
export const METRIC_PAYLOADS = [
  "backtest_stats",
  "backtest_compare",
  "backtest_list",
  "backtest_page",
  "portfolio_combined_stats",
  "portfolio_summary",
  "portfolio_contributions",
  "live_account_stats",
  "optimization_panel_stats",
] as const;

export type MetricPayload = (typeof METRIC_PAYLOADS)[number];

export const PERCENT_METRIC_SCALES_BY_PAYLOAD = {
  // GET /api/backtest/{id}/stats — viewer/core.py::_stats_payload_from_result
  // multiplies the canonical fractions by 100 before serving them.
  backtest_stats: {
    net_profit_pct: "points",
    win_rate: "points",
    max_drawdown_pct: "points",
    max_drawdown: "not-applicable",
    cagr: "points",
    psr: "fraction",
    total_return: "fraction",
  },
  // GET /api/backtests/compare — viewer/main.py::_backtest_compare_payload.
  // total_return is the only key left as the engine's fraction; psr is
  // percent points here and a fraction in every other payload.
  backtest_compare: {
    net_profit_pct: "not-applicable",
    win_rate: "points",
    max_drawdown_pct: "not-applicable",
    max_drawdown: "points",
    cagr: "points",
    psr: "points",
    total_return: "fraction",
  },
  // GET /api/backtests/all — viewer/main.py::_backtest_all_item_from_meta
  // serves the canonical stats unchanged, so everything is a fraction.
  backtest_list: {
    net_profit_pct: "not-applicable",
    win_rate: "fraction",
    max_drawdown_pct: "not-applicable",
    max_drawdown: "fraction",
    cagr: "fraction",
    psr: "fraction",
    total_return: "fraction",
  },
  // GET /api/backtests — viewer/main.py::_backtest_summary_from_meta. The
  // hybrid: percent points for the values it rescales, a normalized fraction
  // for win_rate, and no total_return key at all.
  backtest_page: {
    net_profit_pct: "points",
    win_rate: "fraction",
    max_drawdown_pct: "points",
    max_drawdown: "not-applicable",
    cagr: "points",
    psr: "fraction",
    total_return: "not-applicable",
  },
  // GET /api/portfolios/{id} — shared/models.py::PortfolioStats, persisted by
  // the portfolio builder from the canonical stats: fractions throughout.
  portfolio_combined_stats: {
    net_profit_pct: "not-applicable",
    win_rate: "fraction",
    max_drawdown_pct: "not-applicable",
    max_drawdown: "fraction",
    cagr: "fraction",
    psr: "fraction",
    total_return: "fraction",
  },
  // GET /api/portfolios — viewer/main.py::_portfolio_summary_from_payload:
  // cagr/win_rate are scaled by 100 while max_drawdown stays a fraction.
  portfolio_summary: {
    net_profit_pct: "points",
    win_rate: "points",
    max_drawdown_pct: "points",
    max_drawdown: "fraction",
    cagr: "points",
    psr: "fraction",
    total_return: "fraction",
  },
  // GET /api/portfolios/{id}/contributions —
  // viewer/main.py::_portfolio_contribution_payload: win_rate is scaled,
  // cagr/max_drawdown/psr are not.
  portfolio_contributions: {
    net_profit_pct: "not-applicable",
    win_rate: "points",
    max_drawdown_pct: "not-applicable",
    max_drawdown: "fraction",
    cagr: "fraction",
    psr: "fraction",
    total_return: "not-applicable",
  },
  // GET /api/live/accounts/{account_id}/stats — win_rate and max_drawdown_pct
  // are percent points. `max_drawdown` IS emitted here, but as an absolute
  // currency amount rather than a percent, which is why its cell is
  // "not-applicable": there is no percent scale to declare for it.
  live_account_stats: {
    net_profit_pct: "not-applicable",
    win_rate: "points",
    max_drawdown_pct: "points",
    max_drawdown: "not-applicable",
    cagr: "not-applicable",
    psr: "not-applicable",
    total_return: "not-applicable",
  },
  // An optimization study's train_stats / test_stats panels —
  // runner/optimizer.py::_canonical_stats: the engine's canonical stats, so
  // every key is a fraction. The panels are the one place where cagr,
  // win_rate and max_drawdown_pct are read under the SAME unit as
  // portfolio_combined_stats and the opposite of the summary payloads.
  optimization_panel_stats: {
    net_profit_pct: "not-applicable",
    win_rate: "fraction",
    max_drawdown_pct: "fraction",
    max_drawdown: "not-applicable",
    cagr: "fraction",
    psr: "fraction",
    total_return: "fraction",
  },
} as const satisfies Record<
  MetricPayload,
  Record<PercentMetricKey, PercentScaleEntry>
>;

export type DeclaredMetricScale = {
  [P in MetricPayload]: { [K in PercentMetricKey]: PercentScaleEntry };
};

/**
 * The declared scale of a key in a payload, or undefined when that payload
 * does not emit the key. Prefer :func:`requireMetricScale` in a page: an
 * `undefined` that reaches `percentScale` silently falls back to "points",
 * which is how a fraction gets rendered 100x too large.
 */
export function metricScale(
  payload: MetricPayload,
  key: PercentMetricKey,
): PercentScale | undefined {
  const entry: PercentScaleEntry =
    PERCENT_METRIC_SCALES_BY_PAYLOAD[payload][key];
  return entry === "not-applicable" ? undefined : entry;
}

/**
 * The declared scale of a key that the payload MUST emit.
 *
 * A key marked "not-applicable" for this payload is a hard error rather than
 * `undefined`: reading it means the page is bound to the wrong payload, and
 * the only two possible outcomes are a 100x error or a blank.
 */
export function requireMetricScale(
  payload: MetricPayload,
  key: PercentMetricKey,
): PercentScale {
  const entry: PercentScaleEntry =
    PERCENT_METRIC_SCALES_BY_PAYLOAD[payload][key];
  if (entry === "not-applicable") {
    throw new Error(
      `${payload} does not emit ${key}; binding a percent scale to it would ` +
        "render a value this payload never sends",
    );
  }
  return entry;
}

/** Convert a raw metric value to percent points according to its scale. */
export function toPercentPoints(
  value: number | null | undefined,
  scale: PercentScale,
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return scale === "fraction" ? value * 100 : value;
}

/** Convert a metric to percent points using the scale its payload declares. */
export function toPercentPointsForKey(
  payload: MetricPayload,
  key: PercentMetricKey,
  value: number | null | undefined,
): number | null {
  return toPercentPoints(value, requireMetricScale(payload, key));
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

/**
 * Win rate for the live payloads, in percent points.
 *
 * Routed through the table rather than guessed: `_live_account_stats_payload`
 * serves percent points, and that is now a declared, probed fact instead of an
 * assumption hidden behind a heuristic.
 */
export function liveWinRateToPoints(
  value: number | null | undefined,
): number | null {
  return toPercentPoints(value, requireMetricScale("live_account_stats", "win_rate"));
}

/** Format percent points as a signed percentage string. */
export function formatPercentPoints(
  points: number | null | undefined,
  digits = 2,
): string {
  if (points == null || !Number.isFinite(points)) return "N/A";
  return `${points >= 0 ? "+" : ""}${points.toFixed(digits)}%`;
}
