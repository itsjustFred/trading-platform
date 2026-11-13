"""Reference-parity port of ``backtesting.py`` `BracketLong` (percent sizing + bracket order).

Provenance
----------
Original implementation: ``scripts/reference_parity/reference_strategies.py``
(``BracketLong``), run as ``FractionalBacktest(data, BracketLong, cash=10_000,
commission=0.0, finalize_trades=True)``. Its recorded trade list is committed at
``tests/parity/fixtures/ref_parity_bracket_long_trades.csv`` and
``tests/parity/test_reference_library_parity.py`` asserts this port reproduces it
trade by trade — entry fills, protective-level fills, quantities, PnL and final
equity are all compared exactly, because this strategy has *no* signal exit.

Rules (identical to the original)
---------------------------------
* ``fast = close.rolling(10).mean()``, ``slow = close.rolling(20).mean()``;
  entry on the strict crossover of ``fast`` above ``slow``
  (``backtesting.lib.crossover``).
* ``tr = max(high - low, |high - prev_close|, |low - prev_close|)`` and
  ``atr = tr.rolling(14).mean()`` (Wilder's true range, simple mean).
* Entry levels are published from the *signal bar*: ``stop = close - 2*ATR``,
  ``target = close * (1 + 5%)``.
* **One entry at a time, at least ``min_spacing=60`` bars apart.** The platform
  signal path is stateless, so the spacing rule (not a position check) is what
  carries this policy; the fixture generator verifies the original's position
  gate never binds on the committed fixture, which makes the two equivalent.
* Sizing: 50% of available equity in notional terms (backtesting.py's
  ``size=0.5``). The port runs with ``max_position_exposure_pct=0.5`` and a
  ``risk_per_trade_pct`` large enough that the notional cap binds, which
  reproduces ``0.5 * equity / fill_price``.

Execution semantics
-------------------
* Entry fills at the next bar's open in both frameworks.
* The stop and the target are the only exits, so every exit is a protective
  fill: both frameworks fill at the level itself (the fixture generator refuses
  a data set on which any bar opens through a level, which is the one case where
  backtesting.py's realistic market/limit fill would differ).
* Same-bar stop-and-target resolves stop-first in both (backtesting.py inserts
  the stop order ahead of the target in its queue; the platform's contract
  declares stop-first).
* Zero commission, so quantities, PnL and final equity are compared exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "ref_bracket_long",
    "version": "1.0",
    "parameters": {
        "n1": 10,
        "n2": 20,
        "atr_len": 14,
        "stop_atr": 2.0,
        "tp_pct": 0.05,
        "min_spacing": 60,
    },
    "tags": {"type": ["sma", "crossover", "bracket", "parity", "reference"]},
    "reference": {
        "library": "backtesting.py",
        "example": "BracketLong (this repository's reference port target)",
        "fixture": "tests/parity/fixtures/ref_parity_bracket_long_trades.csv",
    },
}


def _true_range(frame: pd.DataFrame) -> pd.Series:
    """Wilder true range; undefined where an input is missing."""
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [(frame["high"] - frame["low"]), (frame["high"] - previous_close).abs(),
         (frame["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)


def _spaced_entries(raw: pd.Series, min_spacing: int) -> pd.Series:
    """Keep a raw entry signal only when it is ``min_spacing`` bars after the last kept one.

    This is the stateless form of the reference strategy's "one position at a
    time, no churn" policy: the platform's signal path has no position state, so
    the spacing carries it.
    """
    flags = np.asarray(raw, dtype=bool)
    kept = np.zeros(len(flags), dtype=bool)
    last_kept: int | None = None
    for index, flag in enumerate(flags):
        if flag and (last_kept is None or index - last_kept >= min_spacing):
            kept[index] = True
            last_kept = index
    return pd.Series(kept, index=raw.index)


def generate_signals(df, params):
    fast_length = int(params.get("n1", 10))
    slow_length = int(params.get("n2", 20))
    atr_length = int(params.get("atr_len", 14))
    stop_atr = float(params.get("stop_atr", 2.0))
    take_profit_pct = float(params.get("tp_pct", 0.05))
    min_spacing = int(params.get("min_spacing", 60))

    close = df["close"]
    fast = close.rolling(fast_length).mean()
    slow = close.rolling(slow_length).mean()
    atr = _true_range(df).rolling(atr_length).mean()

    crossed_up = (fast.shift(1) < slow.shift(1)) & (fast > slow)
    entries = _spaced_entries(crossed_up.fillna(False), min_spacing)
    exits = pd.Series(False, index=df.index)

    stop_loss = close - stop_atr * atr
    take_profit = close * (1.0 + take_profit_pct)

    indicators = {
        "fast": fast,
        "slow": slow,
        "atr": atr,
        # Level columns the engine reads from the signal frame (see
        # runner/engine_pkg/trading_engine.py::_extract_levels).
        "stop_loss": stop_loss.where(entries),
        "take_profit": take_profit.where(entries),
        "direction": pd.Series("LONG", index=df.index).where(entries, other=""),
    }
    return entries, exits, indicators, []