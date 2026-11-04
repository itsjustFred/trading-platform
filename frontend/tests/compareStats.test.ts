import assert from "node:assert/strict";
import test from "node:test";

import { compareRow } from "../src/lib/compareStats.ts";

test("a measured pair keeps its delta and its direction", () => {
  assert.deepEqual(compareRow(2, 5, "higher"), {
    left: 2,
    right: 5,
    delta: 3,
    betterSecond: true,
  });
  assert.equal(compareRow(5, 2, "higher").betterSecond, false);
  assert.equal(compareRow(5, 2, "lower").betterSecond, true);
});

test("an unavailable side has no delta and no winner", () => {
  // Subtracting a null side as 0 reported a change that was never measured.
  for (const row of [
    compareRow(null, 5, "higher"),
    compareRow(5, null, "lower"),
    compareRow(null, null, "higher"),
  ]) {
    assert.equal(row.delta, null);
    assert.equal(row.betterSecond, false);
  }
});

test("a flat delta is not a win", () => {
  const row = compareRow(3, 3, "higher");
  assert.equal(row.delta, 0);
  assert.equal(row.betterSecond, false);
});
