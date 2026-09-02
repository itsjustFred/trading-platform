import assert from "node:assert/strict";
import test from "node:test";

import { formatStatCardValue, resolveStatCardColor } from "../src/lib/statFormat.ts";

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

test("non-numeric values fall back to their string form", () => {
  assert.equal(formatStatCardValue("n/a", "currency"), "n/a");
  assert.equal(formatStatCardValue("", "percent"), "");
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
