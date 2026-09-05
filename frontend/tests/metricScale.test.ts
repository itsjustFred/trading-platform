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
