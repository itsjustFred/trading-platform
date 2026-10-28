import assert from "node:assert/strict";
import test from "node:test";

import { describeError } from "../src/lib/errorMessage.ts";

test("a real message is preferred over the fallback", () => {
  assert.equal(
    describeError(new Error("HTTP 500 from the broker"), "Could not load"),
    "HTTP 500 from the broker",
  );
});

test("an empty Error message falls back instead of rendering nothing", () => {
  // The defect: `new Error("")` is an Error, so the naive ternary returned "".
  // Callers branch on truthiness, so the error never rendered and the UI showed
  // its loading state forever.
  assert.equal(describeError(new Error(""), "Could not load"), "Could not load");
});

test("a whitespace-only message is treated as empty", () => {
  // Truthy, so `&& error.message` guards let this through, but it renders as
  // nothing. The reason the rule is trim-based rather than truthiness-based.
  assert.equal(describeError(new Error("   "), "Could not load"), "Could not load");
});

test("a non-Error value falls back", () => {
  assert.equal(describeError("boom", "Could not load"), "Could not load");
  assert.equal(describeError(null, "Could not load"), "Could not load");
  assert.equal(describeError(undefined, "Could not load"), "Could not load");
  assert.equal(describeError({ message: "hi" }, "Could not load"), "Could not load");
});

test("a real message is never lost, even when padded", () => {
  // Padding is the operator's; only an all-whitespace message is discarded.
  assert.equal(describeError(new Error("  429  "), "Could not load"), "  429  ");
});
