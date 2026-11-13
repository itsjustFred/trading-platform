from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signal_bots.ramjet import RamJetSignalProcessor


def _sample_market_data(rows: int = 80) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    base = np.linspace(100.0, 120.0, rows)
    close = base + np.sin(np.linspace(0.0, 6.0, rows))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "Open": close - 0.2,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(rows, 10_000),
        }
    )


def test_process_signal_ports_ramjet_sl_tp_and_trailing_config():
    processor = RamJetSignalProcessor(
        {
            "ram_jet": {
                "atr_period": 14,
                "sl_multiplier": 2.0,
                "tp1_multiplier": 3.0,
                "tp1_exit_percent": 0.5,
                "supertrend_period": 10,
                "supertrend_multiplier": 3.0,
                "candle_timeframe": "5Min",
            }
        }
    )
    market_data = _sample_market_data()

    signal = processor.process_signal(
        {
            "symbol": "aapl",
            "action": "BUY",
            "timestamp": "2026-01-01T12:00:00Z",
            "signal_price": 111.25,
        },
        market_data,
    )

    atr = float(signal.metadata["atr"])
    expected_entry = float(market_data["Close"].iloc[-1])
    assert signal.action == "buy"
    assert signal.symbol == "AAPL"
    assert signal.timeframe == "5m"
    assert signal.entry_price == pytest.approx(expected_entry)
    assert signal.stop_loss == pytest.approx(expected_entry - (atr * 2.0))
    assert signal.take_profit == pytest.approx(expected_entry + (atr * 3.0))
    assert signal.metadata["tp1_price"] == pytest.approx(signal.take_profit)
    assert signal.metadata["tp1_exit_percent"] == pytest.approx(0.5)
    assert signal.metadata["signal_price"] == pytest.approx(111.25)

    trailing = signal.metadata["trailing_sl_config"]
    # #150 (decided behaviour change): the mode is a config setting with a
    # deliberate default of 'none' (initial stop only) until #167/#168 land.
    # The old hard-coded 'supertrend_flip' asked for a workflow no path runs.
    assert trailing == {"mode": "none"}
    # No workflow fields that would promise trailing the paths do not run.
    assert "breakeven_after_tp1" not in trailing
    assert "activation_phase" not in trailing
    assert "exit_phase" not in trailing
    # TP1/supertrend context metadata stays: computed facts, not workflow claims.
    assert signal.metadata["tp1_price"] == pytest.approx(signal.take_profit)
    assert "supertrend_green_at_entry" in signal.metadata
    assert "supertrend_ready" in signal.metadata


def test_process_signal_selects_trailing_mode_via_config():
    """#150: the trailing mode comes from config, not from hard-coding."""
    processor = RamJetSignalProcessor(
        {"ram_jet": {"trailing_mode": "percent", "trailing_percent": 0.02}}
    )
    signal = processor.process_signal(
        {"symbol": "AAPL", "action": "BUY"}, _sample_market_data()
    )
    assert signal.metadata["trailing_sl_config"] == {
        "mode": "percent",
        "percent": 0.02,
    }


def test_process_signal_emits_supertrend_flip_config_for_known_mode():
    """#150: supertrend_flip is known, so the processor builds and emits its
    workflow description; executing paths reject the signal (#168 enables it
    by extending the support sets)."""
    processor = RamJetSignalProcessor({"ram_jet": {"trailing_mode": "supertrend_flip"}})
    signal = processor.process_signal(
        {"symbol": "AAPL", "action": "BUY"}, _sample_market_data()
    )
    trailing = signal.metadata["trailing_sl_config"]
    assert trailing["mode"] == "supertrend_flip"
    assert trailing["breakeven_after_tp1"] is True
    assert trailing["activation_phase"] == "wait_for_first_green_after_tp1"
    assert trailing["exit_phase"] == "exit_on_first_red_after_activation"


def test_unknown_trailing_mode_rejected_at_config_time():
    with pytest.raises(ValueError, match="Unknown trailing stop mode"):
        RamJetSignalProcessor({"ram_jet": {"trailing_mode": "martingale_trail"}})


def test_percent_trailing_mode_requires_trailing_percent():
    with pytest.raises(ValueError, match="trailing_percent"):
        RamJetSignalProcessor({"ram_jet": {"trailing_mode": "percent"}})


def test_trailing_percent_rejected_for_other_modes():
    with pytest.raises(ValueError, match="trailing_percent"):
        RamJetSignalProcessor(
            {"ram_jet": {"trailing_mode": "none", "trailing_percent": 0.02}}
        )


def test_trailing_percent_out_of_range_rejected():
    with pytest.raises(ValueError, match="trailing_percent"):
        RamJetSignalProcessor(
            {"ram_jet": {"trailing_mode": "percent", "trailing_percent": 1.5}}
        )


def test_process_signal_rejects_non_buy_actions():
    processor = RamJetSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}})
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(
            {"symbol": "AAPL", "action": "SELL"},
            _sample_market_data(),
        )


def test_processor_loads_default_ramjet_config_file():
    config_path = (
        Path(__file__).resolve().parent.parent / "signal_bots" / "ramjet" / "config.yaml"
    )
    processor = RamJetSignalProcessor.from_config_file(config_path)
    assert processor.atr_period == 14
    assert processor.sl_multiplier == pytest.approx(2.0)
    assert processor.tp1_multiplier == pytest.approx(3.0)
    assert processor.tp1_exit_percent == pytest.approx(0.5)
    assert processor.default_timeframe == "5m"


class _FakeSupertrendTA:
    """Stands in for the ``pandas_ta`` accessor with a deterministic frame."""

    def __init__(self, directions: list[int], period: int, multiplier: float):
        self._directions = directions
        self._period = period
        self._multiplier = multiplier

    def supertrend(self, length=None, multiplier=None, **_kwargs):
        return pd.DataFrame(
            {
                f"SUPERTd_{self._period}_{float(self._multiplier)}": self._directions,
            }
        )


def test_pandas_ta_supertrend_marks_warmup_not_ready(monkeypatch):
    """The pandas_ta branch must carry the same warm-up marker as the fallback.

    Old behaviour: the pandas_ta branch only set ``supertrend``, so
    process_signal fell into its ``else True`` and reported the warm-up bar as
    ready with whatever direction the library emitted for it — the marker was
    the fallback's secret, not the contract's (CodeRabbit batch review).
    """
    period = 100
    rows = 40
    # The library reports green on every bar, but the frame is shorter than
    # the requested warm-up — exactly the short live fetch the golden test
    # pins for the fallback path.
    directions = [1] * rows
    # pandas_ta registers the ``ta`` accessor on import; it is absent when the
    # library is not installed, hence raising=False.
    monkeypatch.setattr(
        pd.DataFrame,
        "ta",
        property(lambda self: _FakeSupertrendTA(directions, period, 3.0)),
        raising=False,
    )

    processor = RamJetSignalProcessor(
        {"ram_jet": {"supertrend_period": period, "candle_timeframe": "5Min"}}
    )
    signal = processor.process_signal({"symbol": "AAPL", "action": "BUY"}, _sample_market_data(rows))

    assert signal.metadata["supertrend_ready"] is False
    assert signal.metadata["supertrend_green_at_entry"] is True

    # Once the warm-up is over the same library output counts as ready.
    warmed_directions = [1] * rows
    monkeypatch.setattr(
        pd.DataFrame,
        "ta",
        property(lambda self: _FakeSupertrendTA(warmed_directions, 10, 3.0)),
        raising=False,
    )
    warmed_processor = RamJetSignalProcessor(
        {"ram_jet": {"supertrend_period": 10, "candle_timeframe": "5Min"}}
    )
    warmed = warmed_processor.process_signal({"symbol": "AAPL", "action": "BUY"}, _sample_market_data(rows))
    assert warmed.metadata["supertrend_ready"] is True


def test_process_signal_fails_closed_without_supertrend_ready_marker(monkeypatch):
    """A Supertrend path that emits no ready marker must not read as ready.

    Old behaviour: a missing ``supertrend_ready`` column defaulted to ``True``,
    so a Supertrend variant that forgot the warm-up marker would silently
    report a fabricated direction as ready (CodeRabbit batch review).
    """

    def _no_marker(df, period, multiplier):
        return df.assign(supertrend=False)

    monkeypatch.setattr(RamJetSignalProcessor, "_calculate_supertrend", staticmethod(_no_marker))
    processor = RamJetSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}})
    signal = processor.process_signal({"symbol": "AAPL", "action": "BUY"}, _sample_market_data())
    assert signal.metadata["supertrend_ready"] is False
