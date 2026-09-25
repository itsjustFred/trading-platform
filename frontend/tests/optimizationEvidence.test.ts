import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  classifyOptimizationOosEvidence,
  resolveCompatibleInSampleValue,
  resolveOptimizationMetricKey,
} from "../src/lib/optimizationEvidence.ts";

function classifyAvailable(
  targetMetric: string,
  inSampleValue: unknown,
  testStats: Record<string, unknown>,
) {
  return classifyOptimizationOosEvidence(
    targetMetric,
    inSampleValue,
    testStats,
    "available",
  );
}

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
  assert.equal(resolveOptimizationMetricKey("sharpe"), "sharpe_ratio");
  assert.equal(resolveOptimizationMetricKey("net_profit"), "total_pnl");
  assert.equal(resolveOptimizationMetricKey("calmar"), "calmar_ratio");
  assert.equal(resolveOptimizationMetricKey("cagr"), "cagr");
  assert.equal(resolveOptimizationMetricKey("win_rate"), "win_rate");
});

test("risk-adjusted objective values require an explicit compatible metric", () => {
  assert.equal(
    resolveCompatibleInSampleValue("net_profit", {
      metric_key: "total_pnl",
      in_sample_value: 500,
      compatible: false,
    }),
    null,
  );
  assert.equal(
    resolveCompatibleInSampleValue("net_profit", {
      metric_key: "sharpe_ratio",
      in_sample_value: 2,
      compatible: true,
    }),
    null,
  );
  assert.equal(
    resolveCompatibleInSampleValue("net_profit", {
      metric_key: "total_pnl",
      in_sample_value: 500,
      compatible: true,
    }),
    500,
  );
});

test("inherited alias names fall back to normalized string metric keys", async () => {
  assert.equal(resolveOptimizationMetricKey("  __PROTO__  "), "__proto__");
  assert.equal(resolveOptimizationMetricKey("Constructor"), "constructor");
});

test("missing, non-numeric, and non-finite target-metric OOS values are unavailable", async () => {
  for (const testStats of [
    {},
    { sharpe_ratio: null },
    { sharpe_ratio: "1.2" },
    { sharpe_ratio: Number.NaN },
    { sharpe_ratio: Number.POSITIVE_INFINITY },
  ]) {
    assert.equal(classifyAvailable("sharpe", 1.2, testStats).state, "unavailable");
  }
});

test("non-finite in-sample values make OOS evidence unavailable", async () => {
  for (const inSampleValue of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    const evidence = classifyAvailable("sharpe", inSampleValue, { sharpe_ratio: 1.2 });

    assert.equal(evidence.state, "unavailable");
    assert.equal(evidence.metricKey, "sharpe_ratio");
    assert.equal(evidence.oosValue, null);
    assert.match(evidence.message, /in-sample selected score.*not finite/i);
  }
});

test("p_value above one is rejected as invalid OOS evidence", async () => {
  const boundary = classifyAvailable("p_value", 0.01, { p_value: 1 });
  const evidence = classifyAvailable("p_value", 0.01, { p_value: 1.01 });

  assert.equal(boundary.label, "OOS significance lost");
  assert.equal(evidence.state, "unavailable");
  assert.equal(evidence.metricKey, "p_value");
  assert.equal(evidence.oosValue, null);
  assert.match(evidence.message, /outside the valid range/i);
});

test("positive in-sample selection degrading to non-positive OOS is explicit", async () => {
  for (const oosValue of [0, -1]) {
    const evidence = classifyAvailable("net_profit", 2500, { total_pnl: oosValue });

    assert.equal(evidence.state, "degraded");
    assert.equal(evidence.metricKey, "total_pnl");
    assert.equal(evidence.oosValue, oosValue);
    assert.match(evidence.message, /positive in-sample.*non-positive OOS/i);
  }
});

test("a zero p-value baseline makes relative OOS evidence unavailable", async () => {
  const evidence = classifyAvailable("p_value", 0, { p_value: 0 });

  assert.equal(evidence.state, "unavailable");
  assert.equal(evidence.metricKey, "p_value");
  assert.equal(evidence.oosValue, null);
  assert.match(evidence.message, /zero.*relative OOS comparison/i);
});

test("finite target-metric OOS evidence is available without a universal negative-value rule", async () => {
  assert.equal(
    classifyAvailable("sharpe", -0.2, { sharpe_ratio: -0.1 }).state,
    "available",
  );
  assert.equal(
    classifyAvailable("win_rate", 0.6, { win_rate: 0.52 }).state,
    "available",
  );
});

test("#87: Sharpe 2.0 -> 0.01 is decayed, not green", async () => {
  const evidence = classifyAvailable("sharpe", 2.0, { sharpe_ratio: 0.01 });

  assert.equal(evidence.state, "decayed");
  assert.match(evidence.message, /less than 50%/i);
});

test("#87: mild OOS dip stays available", async () => {
  assert.equal(
    classifyAvailable("sharpe", 2.0, { sharpe_ratio: 1.5 }).state,
    "available",
  );
});

test("#87: p_value losing significance is degraded, doubling is decayed", async () => {
  assert.equal(
    classifyAvailable("p_value", 0.01, { p_value: 0.5 }).state,
    "degraded",
  );

  assert.equal(
    classifyAvailable("p_value", 0.01, { p_value: 0.03 }).state,
    "decayed",
  );
});

test("negative baselines compare decay in the favorable direction", () => {
  assert.equal(
    classifyAvailable("sharpe", -0.2, { sharpe_ratio: -0.1 }).state,
    "available",
  );
  assert.equal(
    classifyAvailable("sharpe", -0.2, { sharpe_ratio: -0.3 }).state,
    "available",
  );
  assert.equal(
    classifyAvailable("sharpe", -0.2, { sharpe_ratio: -0.5 }).state,
    "decayed",
  );
  assert.equal(
    classifyAvailable("sharpe", -0.2, { sharpe_ratio: 0 }).state,
    "degraded",
  );
});

test("non-available OOS lifecycle status is unavailable", () => {
  assert.equal(
    classifyOptimizationOosEvidence(
      "sharpe",
      2,
      { sharpe_ratio: 2 },
      "insufficient_overlap",
    ).state,
    "unavailable",
  );
  assert.equal(
    classifyOptimizationOosEvidence(
      "sharpe",
      2,
      { sharpe_ratio: 2 },
      "no_oos_requested",
    ).state,
    "unavailable",
  );
});

test("missing and invalid OOS lifecycle statuses fail closed", () => {
  const missing = classifyOptimizationOosEvidence("sharpe", 2, { sharpe_ratio: 2 });
  const invalid = classifyOptimizationOosEvidence(
    "sharpe",
    2,
    { sharpe_ratio: 2 },
    "avaliable",
  );

  assert.equal(missing.state, "unavailable");
  assert.match(missing.message, /OOS status.*unavailable/i);
  assert.equal(invalid.state, "unavailable");
  assert.match(invalid.message, /OOS status.*invalid/i);
});

test("unsupported target metrics cannot produce available evidence", async () => {
  const evidence = classifyAvailable("unknown_metric", 2, {
    unknown_metric: 2,
  });

  assert.equal(evidence.state, "unavailable");
  assert.match(evidence.message, /target metric.*unsupported/i);
});

test("out-of-domain selected and OOS metric values are unavailable", async () => {
  for (const [metric, inSampleValue, oosValue] of [
    ["p_value", 1.01, 0.5],
    ["psr", 0.8, 1.01],
    ["win_rate", -0.1, -0.05],
    ["profit_factor", -1, -0.5],
  ] as const) {
    const evidence = classifyAvailable(metric, inSampleValue, {
      [metric]: oosValue,
    });

    assert.equal(evidence.state, "unavailable", `${metric} must be rejected`);
    assert.match(evidence.message, /outside the valid range/i);
  }
});

test("p_value significance boundaries count as lost significance", () => {
  const inSampleBoundary = classifyAvailable("p_value", 0.05, { p_value: 0.06 });
  const oosBoundary = classifyAvailable("p_value", 0.01, { p_value: 0.05 });

  assert.equal(inSampleBoundary.state, "degraded");
  assert.match(inSampleBoundary.message, /becomes insignificant OOS/i);
  assert.equal(oosBoundary.state, "degraded");
  assert.match(oosBoundary.message, /becomes insignificant OOS/i);
});
