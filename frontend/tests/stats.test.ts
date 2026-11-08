import assert from "node:assert/strict";
import test from "node:test";

import { formatPValue, pValueColor } from "../src/utils/stats.ts";

test("an unavailable p-value is not rendered as the best attainable value", () => {
  // `Number(null)` is 0, which used to render an unmeasured p-value as a
  // significant green "0.000" - the most reassuring reading of an absence.
  for (const value of [
    null,
    undefined,
    "",
    Number.NaN,
    Number.POSITIVE_INFINITY,
  ]) {
    assert.equal(formatPValue(value), "N/A");
    assert.equal(pValueColor(value), "var(--color-text-muted)");
  }
});

test("a measured p-value keeps its formatting and grade", () => {
  assert.equal(formatPValue(0.04), "0.040");
  assert.equal(pValueColor(0.04), "#3fb950");
  assert.equal(formatPValue(0.2), "0.200");
  assert.equal(pValueColor(0.2), "#f85149");
});
