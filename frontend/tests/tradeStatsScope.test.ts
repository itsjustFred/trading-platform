import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { stripComments } from "./sourceRegion.ts";

const pageSource = readFile(
  new URL("../src/pages/BacktestDetailPage.tsx", import.meta.url),
  "utf8",
);

test("page-scoped trade stats are labeled, not presented as whole-backtest", async () => {
  // Comments are stripped where the source is consumed. A whole-file presence
  // assertion is satisfied by a comment, so `stats?.total_trades` - or the
  // `Math.max(...trades.map` this file also asserts is *absent* - could be
  // reintroduced as prose and this file would pass.
  const source = stripComments(await pageSource);

  // The page must know whether stats cover the whole backtest.
  assert.match(source, /statsAreWholeBacktest/);
  // Whole-backtest numbers come from the stats payload fields.
  assert.match(source, /stats\?\.total_trades/);
  assert.match(source, /stats\?\.winners/);
  // Page-derived long/short counts must be labeled as page-scoped.
  assert.match(source, /loaded trades/);
});

test("best/worst trade render N/A instead of page-scoped stand-ins", async () => {
  const source = stripComments(await pageSource);

  // The old code silently fell back to Math.max/min over the loaded page.
  assert.doesNotMatch(source, /Math\.max\(\.\.\.trades\.map/);
  assert.match(source, /tradeStats\.bestTrade != null/);
  assert.match(source, /tradeStats\.worstTrade != null/);
});

test("PSR renders as a fraction per the backend contract", async () => {
  const { metricScale } = await import("../src/api/metricScale.ts");
  const { toPercentPoints } = await import("../src/api/metricScale.ts");
  const source = stripComments(await pageSource);

  // The stats payload serves psr as a probability (0..1); the card must convert
  // it, not render 0.95 as "0.95%". The unit is declared per payload, and this
  // page reads `backtest_stats`, so the call has to name that payload.
  assert.equal(metricScale("backtest_stats", "psr"), "fraction");
  assert.equal(toPercentPoints(0.95, "fraction"), 95);
  assert.match(
    source,
    /label="PSR \(higher = better\)"[\s\S]{0,200}requireMetricScale\("backtest_stats", "psr"\)/,
  );
});
