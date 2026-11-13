"""Reference implementations of the three parity strategies, in backtesting.py.

These are the *original* implementations: they run unchanged on the reference
framework (``backtesting`` >= 0.6, AGPL-3.0) against the generated fixture data.
The commit-fixture workflow (:mod:`generate_reference_parity_fixtures`) runs them,
records their trade lists, and the commit-pinned pytest suite in
``tests/parity/`` asserts the trading-platform native engine reproduces those
trade lists trade for trade.

Nothing here imports anything from ``runner/``: the reference must stay
independent for the differential comparison to mean anything.

Semantics that matter for the comparison (all documented in
``docs/reports/parity-reference-libraries.md``):

* A market order created in ``Strategy.next()`` on bar *N* fills at the **open of
  bar N+1** (``trade_on_close=False``, the default). A ``position.close()`` order
  is also a market order, so a *signal exit* also fills at the next bar's open.
* A contingent stop order is a market-stop: when the bar's low pierces the stop
  the fill is ``min(bar_open, stop_level)`` for a long, i.e. gap-throughs fill at
  the open (worse), otherwise exactly at the level.
* A contingent take-profit order is a limit order: the fill is
  ``max(bar_open, take_profit_level)`` for a long, i.e. a gap through the target
  fills *better* than the level, otherwise exactly at it.
* Both contingent orders live in the broker queue with the stop inserted first,
  so a bar that pierces stop and target resolves stop-first.
* ``size`` between 0 and 1 is a fraction of *available liquidity* (equity minus
  used margin), **not** a risk budget: the order is filled with
  ``floor(equity_available * size / (fill_price + commission_per_unit))`` units.
* ``commission`` is charged per filled order (entry and exit) as
  ``fixed + abs(size) * price * rate``.
* ``FractionalBacktest(fractional_unit=1e-8)`` scales the price series by 1e-8 so
  the same whole-unit machinery trades fractional shares; PnL, commissions and
  equity stay in real currency units, and a size of ``n`` in the transformed
  frame is ``n / 1e8`` real units.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import FractionalBacktest, crossover
from backtesting.test import SMA

#: backtesting.py's documented quickstart parameters (n1=10, n2=20).
SMACROSS_N1 = 10
SMACROSS_N2 = 20
#: Cash used by every reference run.
REFERENCE_CASH = 10_000.0
#: Per-order relative commission of the quickstart run.
QUICKSTART_COMMISSION = 0.002
#: Fractional unit used so the reference can trade fractional shares. The
#: framework rescales ``Size`` back to real units in ``FractionalBacktest.run()``,
#: so the recorded share count is directly comparable with the platform's.
FRACTIONAL_UNIT = 1 / 100e6


def true_range(frame: pd.DataFrame) -> pd.Series:
    """Wilder true range on a title-cased OHLC frame (backtesting.py convention)."""
    prev_close = frame["Close"].shift(1)
    return pd.concat(
        [(frame["High"] - frame["Low"]), (frame["High"] - prev_close).abs(),
         (frame["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


class SmaCross(Strategy):
    """``backtesting.py`` README/quickstart ``SmaCross``: all-in on every golden cross.

    Entry signal: ``SMA(n1)`` crosses above ``SMA(n2)``. Exit signal: the mirror
    cross, executed with ``self.position.close()``.
    """

    n1 = SMACROSS_N1
    n2 = SMACROSS_N2

    def init(self):
        self.sma1 = self.I(SMA, self.data.Close, self.n1)
        self.sma2 = self.I(SMA, self.data.Close, self.n2)

    def next(self):
        if crossover(self.sma1, self.sma2):
            self.buy()
        elif crossover(self.sma2, self.sma1):
            self.position.close()


class BracketLong(Strategy):
    """Long-only SMA-cross entry with percent-of-equity sizing and a bracket order.

    * ``size=0.5`` — 50% of available liquidity (notional sizing, not risk sizing).
    * ``sl = close - stop_atr * ATR(atr_len)``, ``tp = close * (1 + tp_pct)``.
    * The stop and the target are the **only** exits (no signal exit at all), so
      the trade list isolates the platform's protective-exit contract from the
      signal-exit fill convention.
    """

    n1 = SMACROSS_N1
    n2 = SMACROSS_N2
    atr_len = 14
    stop_atr = 2.0
    tp_pct = 0.05
    size_pct = 0.5
    #: Minimum number of bars between two entry signals. The platform port is
    #: stateless, so this spacing (not the position state) is what keeps the
    #: two from double-entering; the fixture generator verifies the position
    #: gate below never binds on top of it.
    min_spacing = 60
    #: One position at a time (realistic portfolio policy).
    gate = True

    def init(self):
        self.sma1 = self.I(SMA, self.data.Close, self.n1)
        self.sma2 = self.I(SMA, self.data.Close, self.n2)
        true_range_series = self.I(true_range, self.data.df)
        self.atr = self.I(
            lambda series: pd.Series(series).rolling(self.atr_len).mean(), true_range_series
        )
        self._last_entry_bar = -10**9

    def next(self):
        if self.gate and (self.position or self.orders):
            return
        close = self.data.Close[-1]
        atr = self.atr[-1]
        if not np.isfinite(atr):
            return
        bar = len(self.data) - 1
        if crossover(self.sma1, self.sma2) and bar - self._last_entry_bar >= self.min_spacing:
            self._last_entry_bar = bar
            self.buy(
                size=self.size_pct,
                sl=close - self.stop_atr * atr,
                tp=close * (1.0 + self.tp_pct),
            )


class BracketShort(Strategy):
    """Short-only mirror of :class:`BracketLong` (exercises short sizing/stops).

    Same parameters, opposite side: the stop sits *above* the entry and the
    target below it, which is the ordering backtesting.py requires for a short
    bracket order.
    """

    n1 = SMACROSS_N1
    n2 = SMACROSS_N2
    atr_len = 14
    stop_atr = 2.0
    tp_pct = 0.04
    size_pct = 0.5
    #: See :class:`BracketLong`.
    min_spacing = 60
    gate = True

    def init(self):
        self.sma1 = self.I(SMA, self.data.Close, self.n1)
        self.sma2 = self.I(SMA, self.data.Close, self.n2)
        true_range_series = self.I(true_range, self.data.df)
        self.atr = self.I(
            lambda series: pd.Series(series).rolling(self.atr_len).mean(), true_range_series
        )
        self._last_entry_bar = -10**9

    def next(self):
        if self.gate and (self.position or self.orders):
            return
        close = self.data.Close[-1]
        atr = self.atr[-1]
        if not np.isfinite(atr):
            return
        bar = len(self.data) - 1
        if crossover(self.sma2, self.sma1) and bar - self._last_entry_bar >= self.min_spacing:
            self._last_entry_bar = bar
            self.sell(
                size=self.size_pct,
                sl=close + self.stop_atr * atr,
                tp=close * (1.0 - self.tp_pct),
            )


#: ``Strategy.buy()`` without a ``size`` argument means "everything the available liquidity
#: allows" (backtesting.py's ``Broker``: ``size`` between 0 and 1 is a fraction of
#: ``margin_available``), which the reference quickstart uses. The bracketed examples pass an
#: explicit ``size=0.5``. These fractions are recorded with every fixture so the committed trade
#: list can be re-derived without the library installed (see
#: ``tests/parity/test_reference_library_parity.py``).
SIZE_FRACTIONS: dict[str, float] = {
    "SmaCross": 1.0,
    "BracketLong": 0.5,
    "BracketShort": 0.5,
}


def reference_size_fraction(strategy: type[Strategy]) -> float:
    """The ``size`` fraction of available liquidity this strategy trades with."""
    return SIZE_FRACTIONS[strategy.__name__]


def _classify_exit(frame: pd.DataFrame, row: pd.Series, forced: bool) -> str:
    """Label one reference exit: ``end_of_data`` / ``stop_loss`` / ``take_profit`` / ``signal``.

    A level exit is recognized by the exit bar piercing the level, not by the
    exit price repeating it: backtesting.py fills a contingent stop at
    ``min(open, level)`` and a contingent target at ``max(open, level)``, so a
    bar that opens through the level produces a fill *different* from the level
    (which is precisely the documented divergence from the platform's
    level-fill contract). A bar that pierces both resolves stop-first, matching
    the reference's order-queue priority.
    """
    if forced:
        return "end_of_data"
    bar = frame.iloc[int(row["ExitBar"])]
    long_side = row["side"] == "LONG"
    stop_level, target_level = row["SL"], row["TP"]
    if pd.notna(stop_level):
        pierced = bar["low"] <= stop_level if long_side else bar["high"] >= stop_level
        if pierced:
            return "stop_loss"
    if pd.notna(target_level):
        pierced = bar["high"] >= target_level if long_side else bar["low"] <= target_level
        if pierced:
            return "take_profit"
    return "signal"

def _run_once(
    strategy: type[Strategy],
    data: pd.DataFrame,
    *,
    commission: float,
    fractional: bool,
    finalize_trades: bool,
) -> tuple[pd.DataFrame, float]:
    factory = FractionalBacktest if fractional else Backtest
    backtest = factory(
        data,
        strategy,
        cash=REFERENCE_CASH,
        commission=commission,
        finalize_trades=finalize_trades,
    )
    stats = backtest.run()
    trades = stats["_trades"].copy()
    trades.index.name = "trade"
    return trades.reset_index(), float(stats["Equity Final [$]"])


def run_reference(
    strategy: type[Strategy],
    frame: pd.DataFrame,
    *,
    commission: float = 0.0,
    fractional: bool = True,
) -> tuple[pd.DataFrame, float]:
    """Run one reference strategy over ``frame`` (a ``timestamp``-indexed OHLCV).

    Returns ``(trades, final_equity)``. ``fractional=True`` uses
    :class:`FractionalBacktest` so sizes are fractional shares; the returned
    ``quantity`` is already rescaled back to real units.

    The run happens twice -- with and without ``finalize_trades`` -- because the
    forced close of a still-open trade (reference: last bar's open) has to be
    distinguishable from a signal exit; trades that only exist in the finalized
    run are recorded as ``exit_kind == "end_of_data"``.
    """
    data = frame.set_index("timestamp").rename(columns=str.title)
    trades, final_equity = _run_once(
        strategy, data, commission=commission, fractional=fractional, finalize_trades=True
    )
    unsettled, _ = _run_once(
        strategy, data, commission=commission, fractional=fractional, finalize_trades=False
    )
    unsettled_keys = set(zip(unsettled["EntryTime"], unsettled["EntryPrice"]))
    forced_keys = {
        key
        for key in zip(trades["EntryTime"], trades["EntryPrice"])
        if key not in unsettled_keys
    }

    # ``FractionalBacktest.run`` rescales ``Size`` back to real units and the
    # prices back to real prices, so the reported size is the share count the
    # platform has to match (``PnL`` there is net of commission; gross is
    # derived below so it lines up with the platform's separate ``pnl``/``fees``).
    trades["side"] = np.where(trades["Size"] >= 0, "LONG", "SHORT")
    trades["quantity"] = trades["Size"].abs()
    trades["gross_pnl"] = trades["PnL"] + trades["Commission"]
    is_forced = np.array(
        [key in forced_keys for key in zip(trades["EntryTime"], trades["EntryPrice"])]
    )
    trades["exit_kind"] = [
        _classify_exit(frame, row, forced)
        for (_, row), forced in zip(trades.iterrows(), is_forced)
    ]
    trades = trades[
        [
            "trade",
            "EntryBar",
            "ExitBar",
            "EntryTime",
            "ExitTime",
            "side",
            "quantity",
            "EntryPrice",
            "ExitPrice",
            "PnL",
            "gross_pnl",
            "Commission",
            "SL",
            "TP",
            "exit_kind",
        ]
    ]
    trades = trades.rename(
        columns={
            "EntryBar": "entry_bar",
            "ExitBar": "exit_bar",
            "EntryTime": "entry_time",
            "ExitTime": "exit_time",
            "EntryPrice": "entry_price",
            "ExitPrice": "exit_price",
            "SL": "stop_level",
            "TP": "take_profit_level",
            "PnL": "net_pnl",
            "gross_pnl": "pnl",
            "Commission": "commission",
        }
    )
    return trades, final_equity
