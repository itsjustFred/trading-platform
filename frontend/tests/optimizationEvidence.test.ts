import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pageSources = Promise.all([
  readFile(new URL("../src/pages/OptimizationsPage.tsx", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/OptimizationResultsPage.tsx", import.meta.url), "utf8"),
]);

test("optimization pages identify scores and parameters as in-sample selected", async () => {
  const [listPage, detailPage] = await pageSources;

  assert.match(listPage, /In-sample selected score/);
  assert.doesNotMatch(listPage, />\s*Best score\s*</);
  assert.match(detailPage, /In-sample selected score/);
  assert.match(detailPage, /In-sample selected parameters/);
  assert.match(listPage, /oos_status \?\? "unavailable"/);
  assert.match(detailPage, /oos_status \?\? "unavailable"/);
  assert.doesNotMatch(detailPage, /Winning envelope|Best parameters card/);
});

test("target metric aliases resolve to the stats keys persisted by the optimizer", async () => {
  const { resolveOptimizationMetricKey } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(resolveOptimizationMetricKey("sharpe"), "sharpe_ratio");
  assert.equal(resolveOptimizationMetricKey("net_profit"), "total_pnl");
  assert.equal(resolveOptimizationMetricKey("calmar"), "calmar_ratio");
  assert.equal(resolveOptimizationMetricKey("cagr"), "cagr");
  assert.equal(resolveOptimizationMetricKey("win_rate"), "win_rate");
});

test("inherited alias names fall back to normalized string metric keys", async () => {
  const { resolveOptimizationMetricKey } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(resolveOptimizationMetricKey("  __PROTO__  "), "__proto__");
  assert.equal(resolveOptimizationMetricKey("Constructor"), "constructor");
});

test("missing, non-numeric, and non-finite target-metric OOS values are unavailable", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  for (const testStats of [
    {},
    { sharpe_ratio: null },
    { sharpe_ratio: "1.2" },
    { sharpe_ratio: Number.NaN },
    { sharpe_ratio: Number.POSITIVE_INFINITY },
  ]) {
    assert.equal(classifyOptimizationOosEvidence("sharpe", 1.2, testStats).state, "unavailable");
  }
});

test("non-finite in-sample values make OOS evidence unavailable", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  for (const inSampleValue of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    const evidence = classifyOptimizationOosEvidence("sharpe", inSampleValue, { sharpe_ratio: 1.2 });

    assert.equal(evidence.state, "unavailable");
    assert.equal(evidence.metricKey, "sharpe_ratio");
    assert.equal(evidence.oosValue, null);
    assert.match(evidence.message, /in-sample selected score.*not finite/i);
  }
});

test("p_value above one is rejected as invalid OOS evidence", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  const boundary = classifyOptimizationOosEvidence("p_value", 0.01, { p_value: 1 });
  const evidence = classifyOptimizationOosEvidence("p_value", 0.01, { p_value: 1.01 });

  assert.equal(boundary.label, "OOS significance lost");
  assert.equal(evidence.state, "degraded");
  assert.equal(evidence.metricKey, "p_value");
  assert.equal(evidence.oosValue, 1.01);
  assert.equal(evidence.label, "OOS significance invalid");
  assert.match(evidence.message, /probability interval 0\.\.1/i);
});

test("positive in-sample selection degrading to non-positive OOS is explicit", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  for (const oosValue of [0, -1]) {
    const evidence = classifyOptimizationOosEvidence("net_profit", 2500, { total_pnl: oosValue });

    assert.equal(evidence.state, "degraded");
    assert.equal(evidence.metricKey, "total_pnl");
    assert.equal(evidence.oosValue, oosValue);
    assert.match(evidence.message, /positive in-sample.*non-positive OOS/i);
  }
});

test("a lower non-negative OOS p-value is not sign degradation", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  const evidence = classifyOptimizationOosEvidence("p_value", 0.05, { p_value: 0 });

  assert.equal(evidence.state, "available");
  assert.equal(evidence.metricKey, "p_value");
  assert.equal(evidence.oosValue, 0);
});

test("finite target-metric OOS evidence is available without a universal negative-value rule", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(
    classifyOptimizationOosEvidence("sharpe", -0.2, { sharpe_ratio: -0.1 }).state,
    "available",
  );
  assert.equal(
    classifyOptimizationOosEvidence("win_rate", 0.6, { win_rate: 0.52 }).state,
    "available",
  );
});

test("#87: Sharpe 2.0 -> 0.01 is decayed, not green", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  const evidence = classifyOptimizationOosEvidence("sharpe", 2.0, { sharpe_ratio: 0.01 });

  assert.equal(evidence.state, "decayed");
  assert.match(evidence.message, /less than 50%/i);
});

test("#87: mild OOS dip stays available", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(
    classifyOptimizationOosEvidence("sharpe", 2.0, { sharpe_ratio: 1.5 }).state,
    "available",
  );
});

test("#87: p_value losing significance is degraded, doubling is decayed", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(
    classifyOptimizationOosEvidence("p_value", 0.01, { p_value: 0.5 }).state,
    "degraded",
  );

  assert.equal(
    classifyOptimizationOosEvidence("p_value", 0.01, { p_value: 0.03 }).state,
    "decayed",
  );
});

test("negative baseline worsening is direction-aware decay", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(
    classifyOptimizationOosEvidence("sharpe", -2, { sharpe_ratio: -3 }).state,
    "decayed",
  );
  assert.equal(
    classifyOptimizationOosEvidence("sharpe", -2, { sharpe_ratio: 0 }).state,
    "degraded",
  );
});

test("non-available OOS lifecycle status is unavailable", async () => {
  const { classifyOptimizationOosEvidence } = await import("../src/lib/optimizationEvidence.ts");

  assert.equal(
    classifyOptimizationOosEvidence("sharpe", 2, { sharpe_ratio: 2 }, "insufficient_overlap").state,
    "unavailable",
  );
  assert.equal(
    classifyOptimizationOosEvidence("sharpe", 2, { sharpe_ratio: 2 }, "no_oos_requested").state,
    "unavailable",
  );
});
