"""Reference-parity port of ``backtesting.py``'s documented `SmaCross` quickstart.

Provenance
----------
Original implementation: ``backtesting.test.SMA`` + the README quickstart
strategy (``SmaCross`` with ``n1=10``, ``n2=20``), run as
``FractionalBacktest(GOOG, SmaCross, cash=10_000, commission=.002,
finalize_trades=True)``. The original runs unchanged in
``scripts/reference_parity/reference_strategies.py``; its recorded trade list is
committed at ``tests/parity/fixtures/ref_parity_smacross_all_in_trades.csv`` and
``tests/parity/test_reference_library_parity.py`` asserts this port reproduces it
trade by trade.

Rules (identical to the original)
---------------------------------
* ``fast = close.rolling(n1).mean()``, ``slow = close.rolling(n2).mean()``
  (backtesting.py's ``SMA`` is exactly ``pd.Series.rolling(n).mean()``).
* Entry: ``fast[-2] < slow[-2]`` and ``fast[-1] > slow[-1]`` — the strict
  crossover of ``backtesting.lib.crossover`` (a tie on the earlier bar is not a
  cross). NaN on either bar is not a cross.
* Exit: the mirror condition, ``slow`` crossing above ``fast``.
* Sizing: all available equity (``size=1`` in backtesting.py == "fraction of
  available liquidity"), i.e. 100% of the engine's position-notional cap. The
  port therefore runs with ``risk_per_trade_pct`` high enough that the
  ``max_position_exposure_pct`` cap is what binds (the risk engine's aggregate
  5% ceiling always sits below it), which reproduces ``equity / fill_price``.

Execution semantics the platform supplies (documented differences, see
``docs/reports/parity-reference-libraries.md``)
-----------------------------------------------------------------------------
* **Entries match exactly**: both fill a market order at the *next bar's open*.
* **Signal exits match exactly**: backtesting.py fills a market close order at
  the *next bar's open*; the platform does the same by default
  (``signal_exit_fill="next_open"``, owner decision 2026-11-13, #208 B).
* **Quantity differs by the commission gross-up**: backtesting.py floors the
  fraction onto ``fill_price + commission_per_unit`` (a broker cannot buy more
  than the cash allows including cost), the platform sizes on the raw notional
  and prices the cost separately (``runner/fees.py``). With a zero-commission
  run the two coincide exactly; the commission run differs by that gross-up
  alone.
"""

from __future__ import annotations

STRATEGY_META = {
    "name": "ref_sma_cross_all_in",
    "version": "1.0",
    "parameters": {"n1": 10, "n2": 20},
    "tags": {"type": ["sma", "crossover", "parity", "reference"]},
    "reference": {
        "library": "backtesting.py",
        "example": "README quickstart SmaCross (n1=10, n2=20)",
        "fixture": "tests/parity/fixtures/ref_parity_smacross_all_in_trades.csv",
    },
}


def generate_signals(df, params):
    fast_length = int(params.get("n1", 10))
    slow_length = int(params.get("n2", 20))

    close = df["close"]
    fast = close.rolling(fast_length).mean()
    slow = close.rolling(slow_length).mean()

    entries = (fast.shift(1) < slow.shift(1)) & (fast > slow)
    exits = (slow.shift(1) < fast.shift(1)) & (slow > fast)

    indicators = {"fast": fast, "slow": slow}
    return entries.fillna(False), exits.fillna(False), indicators, []
