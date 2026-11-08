import assert from "node:assert/strict";
import test from "node:test";

import { applyFillToPosition } from "../src/lib/livePositionMath.ts";
import type { PositionState } from "../src/lib/livePositionMath.ts";

const emptyPositions = () => new Map<string, PositionState>();
const noStats = { totalPnl: 0, winningTrades: 0, losingTrades: 0 };

test("an opening fill records the position and realizes nothing", () => {
  const positions = emptyPositions();

  const stats = applyFillToPosition(positions, "EURUSD", 2, 1.1);

  assert.deepEqual(stats, noStats);
  assert.deepEqual(positions.get("EURUSD"), { qty: 2, avg: 1.1 });
});

test("adding to a position averages the entry price", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", 2, 100);
  applyFillToPosition(positions, "EURUSD", 2, 110);

  // 2 @ 100 + 2 @ 110 -> 4 @ 105, no realized PnL on an increasing position.
  assert.deepEqual(positions.get("EURUSD"), { qty: 4, avg: 105 });
});

test("closing a long realizes the price move as a win", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", 1, 100);
  const stats = applyFillToPosition(positions, "EURUSD", -1, 110);

  assert.equal(stats.totalPnl, 10);
  assert.equal(stats.winningTrades, 1);
  assert.equal(stats.losingTrades, 0);
  assert.deepEqual(positions.get("EURUSD"), { qty: 0, avg: 0 });
});

test("closing a short realizes the inverted price move", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", -1, 110);
  const stats = applyFillToPosition(positions, "EURUSD", 1, 100);

  // Short from 110 covered at 100 is +10.
  assert.equal(stats.totalPnl, 10);
  assert.equal(stats.winningTrades, 1);
});

test("a losing close is counted as a loss, not a win", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", 1, 100);
  const stats = applyFillToPosition(positions, "EURUSD", -1, 90);

  assert.equal(stats.totalPnl, -10);
  assert.equal(stats.winningTrades, 0);
  assert.equal(stats.losingTrades, 1);
});

test("a breakeven close counts as neither a win nor a loss", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", 1, 100);
  const stats = applyFillToPosition(positions, "EURUSD", -1, 100);

  assert.equal(stats.totalPnl, 0);
  assert.equal(stats.winningTrades, 0);
  assert.equal(stats.losingTrades, 0);
});

test("reversing through zero realizes the closed part and re-bases the average", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", 1, 100);
  // Sell 3: closes the long at +10, then opens a short of 2 at the fill price.
  const stats = applyFillToPosition(positions, "EURUSD", -3, 110);

  assert.equal(stats.totalPnl, 10);
  assert.deepEqual(positions.get("EURUSD"), { qty: -2, avg: 110 });
});

test("an oversized close only realizes the closed quantity", () => {
  const positions = emptyPositions();

  applyFillToPosition(positions, "EURUSD", -1, 110);
  // Buy 3 against a short of 1: only 1 unit closes, at +10, leaving a long 2.
  const stats = applyFillToPosition(positions, "EURUSD", 3, 100);

  assert.equal(stats.totalPnl, 10);
  assert.equal(stats.winningTrades, 1);
  assert.deepEqual(positions.get("EURUSD"), { qty: 2, avg: 100 });
});
