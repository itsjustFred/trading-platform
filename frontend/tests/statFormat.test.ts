import assert from "node:assert/strict";
import test from "node:test";

import {
  formatStatCardValue,
  isUnavailableStatValue,
  resolveStatCardColor,
} from "../src/lib/statFormat.ts";

test("currency formatting keeps sign off the scaled magnitude", () => {
  assert.equal(formatStatCardValue(1234, "currency"), "$1,234");
  assert.equal(formatStatCardValue(-1234, "currency"), "-$1,234");
  assert.equal(formatStatCardValue("1234.56", "currency"), "$1,235");
});

test("percent and number formatting round-trip through Number semantics", () => {
  assert.equal(formatStatCardValue(12.345, "percent"), "12.35%");
  assert.equal(formatStatCardValue("42", "number"), "42");
  assert.equal(formatStatCardValue("42", "none"), "42");
});

test("non-numeric labels pass through, empty values become unavailable", () => {
  assert.equal(formatStatCardValue("n/a", "currency"), "n/a");
  // An empty value used to render as "", which is indistinguishable from a
  // blank/zero tile. It now renders as the explicit unavailable marker.
  assert.equal(formatStatCardValue("", "percent"), "—");
});

test("color resolution is sign-based only for numeric currency/percent", () => {
  assert.equal(resolveStatCardColor(undefined, 5, "currency"), "#3fb950");
  assert.equal(resolveStatCardColor(undefined, -0.5, "percent"), "#f85149");
  assert.equal(resolveStatCardColor(undefined, 0, "currency"), "#c9d1d9");
  assert.equal(resolveStatCardColor(undefined, 5, "number"), "#c9d1d9");
  assert.equal(resolveStatCardColor("#ff0000", -5, "currency"), "#ff0000");
  assert.equal(resolveStatCardColor(undefined, Number.NaN, "currency"), "#c9d1d9");
  assert.equal(resolveStatCardColor(undefined, "abc", "currency"), "#c9d1d9");
});

import { stringifyUnknownValue } from "../src/lib/unknownValue.ts";

test("unknown-value stringification never yields [object Object]", () => {
  assert.equal(stringifyUnknownValue("abc"), "abc");
  assert.equal(stringifyUnknownValue(12), "12");
  assert.equal(stringifyUnknownValue(true), "true");
  assert.equal(stringifyUnknownValue(null), "");
  assert.equal(stringifyUnknownValue(undefined, "—"), "—");
  assert.equal(stringifyUnknownValue({ a: 1 }), '{"a":1}');
  assert.equal(stringifyUnknownValue([1, 2]), "[1,2]");
});

test("unavailable metrics never render as numeric zero", () => {
  // The whole point: "we could not compute this" must not look like "zero".
  for (const missing of [null, undefined, "", "   "]) {
    assert.equal(formatStatCardValue(missing, "currency"), "—");
    assert.equal(formatStatCardValue(missing, "percent"), "—");
    assert.equal(formatStatCardValue(missing, "number"), "—");
    assert.equal(formatStatCardValue(missing, "none"), "—");
  }
  assert.equal(formatStatCardValue(Number.NaN, "currency"), "—");
  assert.equal(formatStatCardValue(Number.POSITIVE_INFINITY, "percent"), "—");
  assert.equal(formatStatCardValue(Number.NEGATIVE_INFINITY, "number"), "—");
});

test("a genuine zero still renders as zero", () => {
  assert.equal(formatStatCardValue(0, "currency"), "$0");
  assert.equal(formatStatCardValue(0, "percent"), "0.00%");
  assert.equal(formatStatCardValue(0, "number"), "0");
  assert.equal(formatStatCardValue("0", "currency"), "$0");
  assert.notEqual(formatStatCardValue(0, "currency"), "—");
});

test("deliberate non-numeric labels pass through unchanged", () => {
  // "n/a" is the caller labelling the metric, not a missing value.
  assert.equal(formatStatCardValue("n/a", "currency"), "n/a");
  assert.equal(formatStatCardValue("pending", "number"), "pending");
});

test("isUnavailableStatValue separates missing from zero and from labels", () => {
  assert.equal(isUnavailableStatValue(null), true);
  assert.equal(isUnavailableStatValue(undefined), true);
  assert.equal(isUnavailableStatValue(""), true);
  assert.equal(isUnavailableStatValue(Number.NaN), true);
  assert.equal(isUnavailableStatValue(0), false);
  assert.equal(isUnavailableStatValue("0"), false);
  assert.equal(isUnavailableStatValue("n/a"), false);
  assert.equal(isUnavailableStatValue(12.5), false);
});

test("unavailable values are never coloured as gains or losses", () => {
  for (const missing of [null, undefined, "", Number.NaN]) {
    assert.equal(resolveStatCardColor(undefined, missing, "currency"), "#c9d1d9");
    assert.equal(resolveStatCardColor(undefined, missing, "percent"), "#c9d1d9");
  }
});

test("no currency or number format can render NaN", () => {
  // StatCard used to format inline, so a deliberate label reached
  // Math.abs/toLocaleString as NaN and rendered "$NaN". It now delegates here.
  for (const label of ["n/a", "pending", "—"]) {
    for (const format of ["currency", "number", "percent"] as const) {
      const out = formatStatCardValue(label, format);
      assert.ok(!out.includes("NaN"), `${format} rendered NaN for ${label}: ${out}`);
    }
  }
});
