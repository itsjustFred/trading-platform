export interface PositionState {
  qty: number;
  avg: number;
}

export interface RealizedStats {
  totalPnl: number;
  winningTrades: number;
  losingTrades: number;
}

/** Folds a fill into an existing position, returning realized PnL from any closed part. */
export function applyFillToPosition(
  positions: Map<string, PositionState>,
  symbol: string,
  delta: number,
  price: number,
): RealizedStats {
  const stats: RealizedStats = {
    totalPnl: 0,
    winningTrades: 0,
    losingTrades: 0,
  };
  const current = positions.get(symbol) ?? { qty: 0, avg: 0 };
  const currentQty = current.qty;
  const currentAvg = current.avg;

  if (currentQty === 0) {
    positions.set(symbol, { qty: delta, avg: price });
    return stats;
  }

  if (currentQty * delta > 0) {
    const combinedQty = Math.abs(currentQty) + Math.abs(delta);
    const nextAvg =
      combinedQty > 0
        ? (Math.abs(currentQty) * currentAvg + Math.abs(delta) * price) /
          combinedQty
        : price;
    positions.set(symbol, { qty: currentQty + delta, avg: nextAvg });
    return stats;
  }

  const closedQty = Math.min(Math.abs(currentQty), Math.abs(delta));
  if (closedQty > 0) {
    const sideSign = currentQty > 0 ? 1 : -1;
    const realizedPnl = (price - currentAvg) * closedQty * sideSign;
    stats.totalPnl += realizedPnl;
    if (realizedPnl > 0) {
      stats.winningTrades += 1;
    } else if (realizedPnl < 0) {
      stats.losingTrades += 1;
    }
  }

  const newQty = currentQty + delta;
  if (newQty === 0) {
    positions.set(symbol, { qty: 0, avg: 0 });
  } else if (currentQty * newQty > 0) {
    positions.set(symbol, { qty: newQty, avg: currentAvg });
  } else {
    positions.set(symbol, { qty: newQty, avg: price });
  }
  return stats;
}

/** Price verdicts whose stored price is not a price (mirrors shared/models.py). */
const UNUSABLE_PRICE_VERDICTS = new Set(["sent_bad", "absent", "derived_unavailable"]);

export interface PricedFill {
  action?: string | null;
  symbol?: string | null;
  actual_price: number | null;
  actual_price_observed?: string | null;
  filled_qty: number;
  execution_time: number;
}

/** Whether a fill's price may be used as a price: a usable verdict and a finite positive number. */
export function fillPriceIsUsable(
  fill: Pick<PricedFill, "actual_price" | "actual_price_observed">,
): boolean {
  if (fill.actual_price_observed && UNUSABLE_PRICE_VERDICTS.has(fill.actual_price_observed)) {
    return false;
  }
  const price = fill.actual_price;
  return typeof price === "number" && Number.isFinite(price) && price > 0;
}

export interface RealizedTradeStats extends RealizedStats {
  /** False when any buy/sell fill has real quantity at an unknown price. */
  complete: boolean;
  unpricedFills: number;
}

/**
 * Realized PnL over fills in execution order.
 *
 * A fill with quantity but no usable price still moved the position, so everything realized after
 * it is unknown: `totalPnl` is NaN and `complete` is false, rather than a smaller number that reads
 * as real (#124, review of #143).
 */
export function calculateRealizedTradeStats(trades: readonly PricedFill[]): RealizedTradeStats {
  const positions = new Map<string, PositionState>();
  const stats: RealizedTradeStats = {
    totalPnl: 0,
    winningTrades: 0,
    losingTrades: 0,
    complete: true,
    unpricedFills: 0,
  };

  const orderedTrades = [...trades].sort((a, b) => a.execution_time - b.execution_time);
  for (const trade of orderedTrades) {
    const action = String(trade.action || "").toLowerCase();
    let direction = 0;
    if (action === "buy") {
      direction = 1;
    } else if (action === "sell") {
      direction = -1;
    }
    if (direction === 0) {
      continue;
    }

    const rawQuantity = Number(trade.filled_qty);
    const quantity = Number.isFinite(rawQuantity) ? Math.abs(rawQuantity) : 0;
    if (quantity <= 0) {
      continue;
    }
    if (!fillPriceIsUsable(trade)) {
      stats.unpricedFills += 1;
      stats.complete = false;
      continue;
    }

    const symbol = String(trade.symbol || "UNKNOWN").toUpperCase();
    const fillStats = applyFillToPosition(
      positions,
      symbol,
      quantity * direction,
      trade.actual_price as number,
    );
    stats.totalPnl += fillStats.totalPnl;
    stats.winningTrades += fillStats.winningTrades;
    stats.losingTrades += fillStats.losingTrades;
  }

  if (!stats.complete) {
    stats.totalPnl = Number.NaN;
  }
  return stats;
}
