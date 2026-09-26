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
