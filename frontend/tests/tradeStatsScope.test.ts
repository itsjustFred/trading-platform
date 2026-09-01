import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const pageSource = readFile(
  new URL("../src/pages/BacktestDetailPage.tsx", import.meta.url),
  "utf8",
);

test("page-scoped trade stats are labeled, not presented as whole-backtest", async () => {
  const source = await pageSource;

  // The page must know whether stats cover the whole backtest.
  assert.match(source, /statsAreWholeBacktest/);
  // Whole-backtest numbers come from the stats payload fields.
  assert.match(source, /stats\?\.total_trades/);
  assert.match(source, /stats\?\.winners/);
  // Page-derived long/short counts must be labeled as page-scoped.
  assert.match(source, /loaded trades/);
});

test("best/worst trade render N/A instead of page-scoped stand-ins", async () => {
  const source = await pageSource;

  // The old code silently fell back to Math.max/min over the loaded page.
  assert.doesNotMatch(source, /Math\.max\(\.\.\.trades\.map/);
  assert.match(source, /tradeStats\.bestTrade != null/);
  assert.match(source, /tradeStats\.worstTrade != null/);
});
