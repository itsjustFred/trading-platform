import assert from "node:assert/strict";
import test from "node:test";

test("metric scale contract converts fractions and points explicitly", async () => {
  const { toPercentPoints, metricScale } = await import(
    "../src/api/metricScale.ts"
  );

  assert.equal(metricScale("win_rate"), "points");
  assert.equal(metricScale("psr"), "fraction");

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

  assert.equal(metricScale("total_return"), "fraction");
  assert.equal(metricScale("net_profit_pct"), "points");
  assert.equal(requireMetricScale("total_return"), "fraction");
  assert.equal(requireMetricScale("net_profit_pct"), "points");

  assert.equal(toPercentPointsForKey("total_return", 0.03125), 3.125);
  assert.equal(toPercentPointsForKey("total_return", -0.125), -12.5);
  assert.equal(toPercentPointsForKey("net_profit_pct", 3.125), 3.125);
  assert.equal(toPercentPointsForKey("total_return", null), null);
  assert.equal(toPercentPointsForKey("total_return", Number.NaN), null);
});

test("no declared key lacks a unit, and none is ambiguous", async () => {
  const { PERCENT_METRIC_SCALES } = await import("../src/api/metricScale.ts");

  for (const [key, scale] of Object.entries(PERCENT_METRIC_SCALES)) {
    assert.ok(
      scale === "fraction" || scale === "points",
      `${key} must declare "fraction" or "points", got ${String(scale)}`,
    );
  }
  // A key no payload emits cannot be held to a unit, so it must not be declared.
  // tests/test_metric_scale_contract.py enforces the backend half of this.
  assert.equal(
    Object.hasOwn(PERCENT_METRIC_SCALES, "total_return_raw"),
    false,
    "total_return_raw is emitted by no payload; drop it or add the producer",
  );
});
