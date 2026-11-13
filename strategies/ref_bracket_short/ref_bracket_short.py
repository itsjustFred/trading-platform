"""Reference-parity port of ``backtesting.py`` `BracketShort` (short side of the bracket port).

Provenance
----------
Original implementation: ``scripts/reference_parity/reference_strategies.py``
(``BracketShort``), run as ``FractionalBacktest(data, BracketShort, cash=10_000,
commission=0.0, finalize_trades=True)``. Its recorded trade list is committed at
``tests/parity/fixtures/ref_parity_bracket_short_trades.csv`` and
``tests/parity/test_reference_library_parity.py`` asserts this port reproduces it
trade by trade — entry fills, protective fills, quantities, PnL and final equity
are compared exactly.

What this port adds over :mod:`ref_bracket_long`
-----------------------------------------------
* the **short** side of the bracket: the stop sits *above* the entry and the
  target below it (the ordering backtesting.py's ``Strategy.sell`` requires),
* SHORT protective fills in the platform engine (stop triggers when
  ``high >= stop``, target when ``low <= target``; both fill at the level),
* SHORT signal-accounting: PnL is ``(entry - exit) * quantity``.

Rules (identical to the original)
---------------------------------
* ``fast = close.rolling(10).mean()``, ``slow = close.rolling(20).mean()``;
  entry on the strict crossover of ``slow`` above ``fast``.
* ``tr = max(high - low, |high - prev_close|, |low - prev_close|)`` and
  ``atr = tr.rolling(14).mean()``.
* Entry levels published from the signal bar: ``stop = close + 2*ATR``,
  ``target = close * (1 - 4%)``.
* **One entry at a time, at least ``min_spacing=60`` bars apart** (see
  :mod:`ref_bracket_long` for why the spacing carries that policy).
* Sizing: 50% of available equity in notional terms, i.e.
  ``max_position_exposure_pct=0.5`` with the risk leg above the cap so the cap
  binds — ``0.5 * equity / fill_price``.
* Zero commission: quantities, PnL and final equity are compared exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

STRATEGY_META = {
    "name": "ref_bracket_short",
    "version": "1.0",
    "parameters": {
        "n1": 10,
        "n2": 20,
        "atr_len": 14,
        "stop_atr": 2.0,
        "tp_pct": 0.04,
        "min_spacing": 60,
    },
    "tags": {"type": ["sma", "crossover", "bracket", "short", "parity", "reference"]},
    "reference": {
        "library": "backtesting.py",
        "example": "BracketShort (this repository's reference port target)",
        "fixture": "tests/parity/fixtures/ref_parity_bracket_short_trades.csv",
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
    """Keep a raw entry signal only when it is ``min_spacing`` bars after the last kept one."""
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
    take_profit_pct = float(params.get("tp_pct", 0.04))
    min_spacing = int(params.get("min_spacing", 60))

    close = df["close"]
    fast = close.rolling(fast_length).mean()
    slow = close.rolling(slow_length).mean()
    atr = _true_range(df).rolling(atr_length).mean()

    crossed_down = (slow.shift(1) < fast.shift(1)) & (slow > fast)
    entries = _spaced_entries(crossed_down.fillna(False), min_spacing)
    exits = pd.Series(False, index=df.index)

    stop_loss = close + stop_atr * atr
    take_profit = close * (1.0 - take_profit_pct)

    indicators = {
        "fast": fast,
        "slow": slow,
        "atr": atr,
        # Level columns the engine reads from the signal frame (see
        # runner/engine_pkg/trading_engine.py::_extract_levels).
        "stop_loss": stop_loss.where(entries),
        "take_profit": take_profit.where(entries),
        "direction": pd.Series("SHORT", index=df.index).where(entries, other=""),
    }
    return entries, exits, indicators, []