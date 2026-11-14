import assert from "node:assert/strict";
import test from "node:test";

import type { BacktestMetadataItem } from "../src/api/types.ts";

function item(overrides: Partial<BacktestMetadataItem> = {}): BacktestMetadataItem {
  return {
    id: "bt-1",
    strategy_name: "s",
    symbols: ["EURUSD"],
    timeframe: "H1",
    total_return: 0.1,
    sharpe_ratio: 1.2,
    psr: 0.9,
    p_value: 0.03,
    cagr: 0.08,
    max_drawdown: 0.2,
    trade_count: 10,
    win_rate: 0.55,
    start_date: "2020-01-01",
    end_date: "2021-01-01",
    created_at: "2021-02-01T00:00:00Z",
    ...overrides,
  };
}

test("measuredOrNull keeps real zeros and drops every non-measurement", async () => {
  const { measuredOrNull } = await import("../src/lib/backtestListMetrics.ts");

  assert.equal(measuredOrNull(0), 0);
  assert.equal(measuredOrNull(-1.5), -1.5);
  assert.equal(measuredOrNull(null), null);
  assert.equal(measuredOrNull(undefined), null);
  assert.equal(measuredOrNull(Number.NaN), null);
  assert.equal(measuredOrNull(Number.POSITIVE_INFINITY), null);
});

test("listMetricsFor maps unmeasured metrics to null instead of zero", async () => {
  const { listMetricsFor } = await import("../src/lib/backtestListMetrics.ts");

  const unmeasured = listMetricsFor(
    item({ sharpe_ratio: null, psr: null, p_value: null, cagr: null, win_rate: null }),
  );
  assert.equal(unmeasured.sharpe, null);
  assert.equal(unmeasured.psr, null);
  assert.equal(unmeasured.pValue, null);
  assert.equal(unmeasured.cagr, null);
  assert.equal(unmeasured.winRate, null);

  const measured = listMetricsFor(item({ total_return: 0.1234, max_drawdown: -0.2 }));
  assert.equal(measured.totalReturn, 12.34);
  assert.equal(measured.maxDrawdown, 20);
});

test("bounds are computed from measured values only, never a phantom zero", async () => {
  const { boundsFromValues } = await import("../src/lib/backtestListMetrics.ts");
  const fallback = { min: -2, max: 3 };

  assert.deepEqual(boundsFromValues([null, 1, 2], fallback), { min: 1, max: 2 });
  assert.deepEqual(boundsFromValues([null, -1, 2], fallback), { min: -1, max: 2 });
  assert.deepEqual(boundsFromValues([null, null], fallback), fallback);
  assert.deepEqual(boundsFromValues([], fallback), fallback);
});

test("an unmeasured value never satisfies a numeric range", async () => {
  const { inNumericRange, passesNumericFilters, listMetricsFor } =
    await import("../src/lib/backtestListMetrics.ts");

  // Range that spans zero: a 0 fallback used to pass, a null must not.
  assert.equal(inNumericRange(null, { min: -2, max: 3 }), false);
  assert.equal(inNumericRange(0, { min: -2, max: 3 }), true);
  // p-value: a null used to be coerced to 1 and could fail the filter for the
  // wrong reason; it must now be excluded regardless of the range.
  assert.equal(inNumericRange(null, { min: 0, max: 1 }), false);

  const full = {
    totalReturn: { min: -1000, max: 1000 },
    sharpe: { min: -2, max: 3 },
    psr: { min: 0, max: 1 },
    pValue: { min: 0, max: 1 },
    cagr: { min: -1000, max: 1000 },
    maxDrawdown: { min: 0, max: 1000 },
    winRate: { min: 0, max: 100 },
  };
  assert.equal(passesNumericFilters(listMetricsFor(item()), full), true);
  assert.equal(
    passesNumericFilters(listMetricsFor(item({ sharpe_ratio: null })), full),
    false,
  );
});

test("sorting keeps unmeasured values last in both directions", async () => {
  const { compareMeasuredLast } = await import("../src/lib/backtestListMetrics.ts");

  // asc (direction 1): smallest first, null after every number.
  assert.ok(compareMeasuredLast(null, 0, 1) > 0);
  assert.ok(compareMeasuredLast(0, null, 1) < 0);
  assert.ok(compareMeasuredLast(-5, null, 1) < 0);
  // desc (direction -1): largest first, null still after every number.
  assert.ok(compareMeasuredLast(null, 0, -1) > 0);
  assert.ok(compareMeasuredLast(0, null, -1) < 0);
  assert.ok(compareMeasuredLast(5, null, -1) < 0);
  // two nulls tie; two numbers order by direction.
  assert.equal(compareMeasuredLast(null, null, 1), 0);
  assert.ok(compareMeasuredLast(1, 2, 1) < 0);
  assert.ok(compareMeasuredLast(1, 2, -1) > 0);
});

test("sortableMetric treats an unmeasured win rate as unmeasured, not as -1", async () => {
  const { sortableMetric } = await import("../src/lib/backtestListMetrics.ts");

  assert.equal(sortableMetric(item({ win_rate: null }), "win_rate"), null);
  assert.equal(sortableMetric(item({ win_rate: 0 }), "win_rate"), 0);
  // Mixed scales (fraction vs percent points) normalize to the same unit.
  assert.equal(sortableMetric(item({ win_rate: 0.55 }), "win_rate"), 0.55);
  assert.equal(sortableMetric(item({ win_rate: 55 }), "win_rate"), 0.55);
  assert.equal(sortableMetric(item({ p_value: null }), "p_value"), null);
  assert.equal(sortableMetric(item({ sharpe_ratio: null }), "sharpe_ratio"), null);
});
