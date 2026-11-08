import assert from "node:assert/strict";
import test from "node:test";

test("metric scale contract converts fractions and points explicitly", async () => {
  const { toPercentPoints, metricScale } = await import(
    "../src/api/metricScale.ts"
  );

  assert.equal(metricScale("backtest_stats", "win_rate"), "points");
  assert.equal(metricScale("backtest_stats", "psr"), "fraction");

  assert.equal(toPercentPoints(0.1234, "fraction"), 12.34);
  assert.equal(toPercentPoints(12.34, "points"), 12.34);
  assert.equal(toPercentPoints(null, "fraction"), null);
  assert.equal(toPercentPoints(Number.NaN, "points"), null);
});

test("normalizeWinRate accepts both scales without double conversion", async () => {
  const { normalizeWinRate } = await import("../src/api/metricScale.ts");

  assert.equal(normalizeWinRate(0.55), 0.55);
  assert.equal(normalizeWinRate(55), 0.55);
  assert.equal(normalizeWinRate(-1), 0);
  assert.equal(normalizeWinRate(150), 1);
  assert.equal(normalizeWinRate(null), null);
});

test("formatPercentPoints renders signed percent strings", async () => {
  const { formatPercentPoints } = await import("../src/api/metricScale.ts");

  assert.equal(formatPercentPoints(12.341), "+12.34%");
  assert.equal(formatPercentPoints(-3.5), "-3.50%");
  assert.equal(formatPercentPoints(null), "N/A");
});

test("total_return is a fraction and net_profit_pct stays in points", async () => {
  // The backend writes total_return as E_end / E_start - 1 (runner/stats.py) and
  // net_profit_pct as percent points. Rendering either with the other's scale is
  // the 100x bug: 0.03125 read as points is "+0.03%", read as a points value
  // with a second *100 is "+312.50%".
  const { metricScale, requireMetricScale, toPercentPointsForKey } = await import(
    "../src/api/metricScale.ts"
  );

  assert.equal(metricScale("backtest_stats", "total_return"), "fraction");
  assert.equal(metricScale("backtest_stats", "net_profit_pct"), "points");
  assert.equal(requireMetricScale("backtest_stats", "total_return"), "fraction");
  assert.equal(requireMetricScale("backtest_stats", "net_profit_pct"), "points");

  assert.equal(
    toPercentPointsForKey("backtest_stats", "total_return", 0.03125),
    3.125,
  );
  assert.equal(
    toPercentPointsForKey("backtest_stats", "total_return", -0.125),
    -12.5,
  );
  assert.equal(
    toPercentPointsForKey("backtest_stats", "net_profit_pct", 3.125),
    3.125,
  );
  assert.equal(toPercentPointsForKey("backtest_stats", "total_return", null), null);
  assert.equal(
    toPercentPointsForKey("backtest_stats", "total_return", Number.NaN),
    null,
  );
});

test("the unit belongs to the payload, not to the key", async () => {
  // The defect this table exists for: win_rate is percent points in the stats
  // payload and a fraction in the list payload, so one flat scale cannot serve
  // both and a page that binds the wrong one renders 100x off.
  const { requireMetricScale, toPercentPointsForKey } = await import(
    "../src/api/metricScale.ts"
  );

  assert.equal(requireMetricScale("backtest_stats", "win_rate"), "points");
  assert.equal(requireMetricScale("backtest_list", "win_rate"), "fraction");
  assert.equal(
    toPercentPointsForKey("backtest_stats", "win_rate", 62.5),
    62.5,
  );
  assert.equal(toPercentPointsForKey("backtest_list", "win_rate", 0.625), 62.5);

  // cagr and max_drawdown split the same way, and psr is points in exactly one
  // payload (the compare payload) and a fraction everywhere else.
  assert.equal(requireMetricScale("backtest_stats", "cagr"), "points");
  assert.equal(requireMetricScale("backtest_list", "cagr"), "fraction");
  assert.equal(requireMetricScale("backtest_compare", "max_drawdown"), "points");
  assert.equal(requireMetricScale("backtest_list", "max_drawdown"), "fraction");
  assert.equal(requireMetricScale("backtest_compare", "psr"), "points");
  assert.equal(requireMetricScale("portfolio_combined_stats", "psr"), "fraction");

  // An optimization panel reads the same three keys as fractions.
  assert.equal(
    requireMetricScale("optimization_panel_stats", "cagr"),
    "fraction",
  );
  assert.equal(
    requireMetricScale("optimization_panel_stats", "win_rate"),
    "fraction",
  );

  // The portfolio's own two payloads disagree about cagr and win_rate, which is
  // why PortfolioDetailPage must name the payload it reads.
  assert.equal(
    requireMetricScale("portfolio_combined_stats", "cagr"),
    "fraction",
  );
  assert.equal(requireMetricScale("portfolio_summary", "cagr"), "points");
  assert.equal(
    requireMetricScale("portfolio_combined_stats", "win_rate"),
    "fraction",
  );
  assert.equal(requireMetricScale("portfolio_summary", "win_rate"), "points");
});

test("reading a key a payload does not emit is a hard error", async () => {
  // The two ways out are both wrong: `undefined` falls back to "points" in
  // StatCard, and a guessed unit renders 100x off. Throwing keeps the wiring
  // bug where it was introduced.
  const { requireMetricScale, metricScale, toPercentPointsForKey } = await import(
    "../src/api/metricScale.ts"
  );

  assert.equal(metricScale("backtest_stats", "max_drawdown"), undefined);
  assert.throws(
    () => requireMetricScale("backtest_stats", "max_drawdown"),
    /backtest_stats does not emit max_drawdown/,
  );
  assert.throws(
    () => toPercentPointsForKey("live_account_stats", "cagr", 0.1),
    /live_account_stats does not emit cagr/,
  );
});

test("every payload declares every key, and every cell is a decision", async () => {
  const {
    PERCENT_METRIC_KEYS,
    PERCENT_METRIC_SCALES_BY_PAYLOAD,
    METRIC_PAYLOADS,
  } = await import("../src/api/metricScale.ts");

  for (const payload of METRIC_PAYLOADS) {
    const table = PERCENT_METRIC_SCALES_BY_PAYLOAD[payload] as Record<
      string,
      string
    >;
    for (const key of PERCENT_METRIC_KEYS) {
      const cell = table[key];
      assert.ok(
        cell === "fraction" || cell === "points" || cell === "not-applicable",
        `${payload}.${key} must declare "fraction", "points" or ` +
          `"not-applicable", got ${String(cell)}`,
      );
    }
    // A key no payload emits cannot be held to a unit, so it must not be
    // declared. tests/test_metric_scale_contract.py enforces the backend half.
    assert.equal(
      Object.hasOwn(table, "total_return_raw"),
      false,
      "total_return_raw is emitted by no payload; drop it or add the producer",
    );
  }
  assert.deepEqual(
    [...METRIC_PAYLOADS].sort(),
    [
      "backtest_compare",
      "backtest_list",
      "backtest_page",
      "backtest_stats",
      "live_account_stats",
      "optimization_panel_stats",
      "portfolio_combined_stats",
      "portfolio_contributions",
      "portfolio_summary",
    ],
    "a new payload must be added here so the contract test probes it",
  );
});
