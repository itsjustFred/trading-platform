import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  parameterFieldId,
  resolveExitChartTime,
  toChartTime,
} from "../src/lib/liveChartTime.ts";

test("chart time accepts positive unix seconds and truncates fractions", () => {
  assert.equal(toChartTime(1_700_000_000.9), 1_700_000_000);
  assert.equal(toChartTime(60), 60);
});

test("chart time rejects zero, negative, and non-finite values", () => {
  for (const value of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(toChartTime(value), null);
  }
});

test("exit marker time prefers the execution time over the signal time", () => {
  // Regression: markers used to be pinned to the signal bar even when the fill
  // happened on a later bar.
  assert.equal(resolveExitChartTime(1_700_000_100, 1_700_000_060), 1_700_000_100);
});

test("exit marker time falls back to the signal time without an execution", () => {
  assert.equal(resolveExitChartTime(0, 1_700_000_060), 1_700_000_060);
  assert.equal(resolveExitChartTime(Number.NaN, 1_700_000_060), 1_700_000_060);
});

test("exit marker time is null when neither source is usable", () => {
  assert.equal(resolveExitChartTime(0, 0), null);
});

test("parameter field ids are unique and stable per parameter name", () => {
  // Regression: every row used to render the same static ids, so all labels
  // pointed at the first row's inputs.
  const ids = new Set(
    ["fast", "slow", "Signal Period", "fast"].map((name) => `${parameterFieldId(name)}-min`),
  );
  assert.equal(ids.size, 3);
  assert.equal(parameterFieldId("fast"), "optimize-param-fast");
  assert.equal(parameterFieldId("  Fast  "), "optimize-param-fast");
  assert.equal(parameterFieldId("Signal Period"), "optimize-param-signal-period");
  assert.equal(parameterFieldId("RSI.length"), "optimize-param-rsi-length");
});

test("parameter field ids stay non-empty for degenerate names", () => {
  assert.equal(parameterFieldId(""), "optimize-param-unnamed");
  assert.equal(parameterFieldId("///"), "optimize-param-unnamed");
});

test("optimize modal labels reference the per-parameter id helper, not static ids", async () => {
  const modal = await readFile(
    new URL("../src/components/OptimizeModal.tsx", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(modal, /htmlFor="optimize-(categories|min|max|step)"/);
  assert.match(modal, /parameterFieldId\(name\)/);
  assert.match(modal, /htmlFor=\{[`]\$\{fieldId\}-min[`]\}/);
});

test("live viewer exit markers resolve time from the execution timestamp", async () => {
  const viewer = await readFile(
    new URL("../src/pages/LiveViewer.tsx", import.meta.url),
    "utf8",
  );

  assert.match(viewer, /exitTimeUnix: asFinite\(row\.execution_time, 0\)/);
  assert.match(viewer, /toChartTime\(trade\.exitTimeUnix\)/);
  assert.doesNotMatch(viewer, /signalTimeUnix/);
});
