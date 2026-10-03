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
    assert trailing["mode"] == "supertrend_flip"
    assert trailing["breakeven_after_tp1"] is True
    assert trailing["activation_phase"] == "wait_for_first_green_after_tp1"
    assert trailing["exit_phase"] == "exit_on_first_red_after_activation"


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


def _config(period: int = 14) -> dict:
    return {
        "ram_jet": {
            "atr_period": period,
            "sl_multiplier": 2.0,
            "tp1_multiplier": 3.0,
            "tp1_exit_percent": 0.5,
            "supertrend_period": 10,
            "supertrend_multiplier": 3.0,
            "candle_timeframe": "5Min",
        }
    }


def _signal() -> dict:
    return {
        "symbol": "aapl",
        "action": "BUY",
        "timestamp": "2026-01-01T12:00:00Z",
        "signal_price": 111.25,
    }


def test_a_frame_shorter_than_atr_period_is_refused_instead_of_stopping_on_it():
    """**The warm-up gap is closed: a short frame is now refused, fail-closed.**

    This used to be pinned as-is, asserting the opposite. The strategies compute
    `tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False)`, so their ATR is `NaN` until
    `period` bars exist and a signal built on it is *impossible*. The live `_calculate_atr`
    omitted `min_periods`, so an EWM yielded a finite, positive number from the very first bar -
    and the guard only rejects a non-finite or non-positive ATR, which an under-warmed one is
    not. Measured on a growing-range series at `atr_period=14`, a 3-bar frame yielded an ATR of
    89.6% of the warmed value, so a materially tighter stop was placed from the same signal, in
    exactly the regime where the backtest declines to produce a number at all.

    `min_periods` is the strategies' own contract rather than a new rule, and the refusal lands
    in the guard that was already there - no new failure mode was invented. The `alpha`
    difference is untouched and remains the open half of item 5.
    """
    processor = RamJetSignalProcessor(_config(period=14))

    for rows in (1, 3, 13):
        short = _sample_market_data(rows=rows)
        with pytest.raises(ValueError, match="Latest ATR value is invalid"):
            processor.process_signal(_signal(), short)

    # Exactly `period` bars is the first frame that can produce an ATR, and it must.
    exact = _sample_market_data(rows=14)
    signal = processor.process_signal(_signal(), exact)
    atr = float(signal.metadata["atr"])
    assert np.isfinite(atr) and atr > 0, (
        "a frame of exactly atr_period bars no longer yields an ATR; `min_periods=period` "
        "should gate the first `period - 1` bars and no more, so this pin is now wrong"
    )
    entry = float(exact["Close"].iloc[-1])
    assert signal.stop_loss == pytest.approx(entry - (atr * 2.0))
    assert signal.stop_loss < entry < signal.take_profit, "the bracket must still straddle entry"


def test_the_refusal_is_the_pre_existing_guard_not_a_new_one():
    """A short frame must reach the guard that already refused an invalid ATR.

    The change is `min_periods`, nothing else. If the refusal arrived by some new route, a caller
    catching this exception would be relying on machinery that did not exist before, and the
    message would no longer be the one that documents the failure.
    """
    processor = RamJetSignalProcessor(_config(period=14))
    with pytest.raises(ValueError) as excinfo:
        processor.process_signal(_signal(), _sample_market_data(rows=3))
    assert "Latest ATR value is invalid for SL/TP calculation" in str(excinfo.value), (
        f"the short-frame refusal is no longer the established guard's: {excinfo.value}"
    )


def test_every_warmed_atr_value_is_bit_identical_to_before():
    """The change must be confined to the warm-up window, and that is checkable.

    `min_periods` gates *whether* a value is produced, not which observations feed the
    recursion, so from bar `period - 1` onward the recursion is untouched. If this fails, the
    edit changed sizing on every signal rather than only refusing the unsupported ones - the
    difference between fixing a warm-up hole and silently re-tuning a strategy.
    """
    period = 14
    frame = _sample_market_data(rows=120).copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    frame["h_l"] = frame["high"] - frame["low"]
    frame["h_pc"] = (frame["high"] - frame["close"].shift(1)).abs()
    frame["l_pc"] = (frame["low"] - frame["close"].shift(1)).abs()
    frame["tr"] = frame[["h_l", "h_pc", "l_pc"]].max(axis=1)

    as_was = frame["tr"].ewm(span=period, adjust=False).mean()
    as_now = frame["tr"].ewm(span=period, min_periods=period, adjust=False).mean()

    assert as_now.iloc[: period - 1].isna().all(), (
        "the warm-up window no longer produces NaN, so short frames are being sized off an "
        "unwarmed ATR again"
    )
    assert np.array_equal(
        as_was.iloc[period - 1:].to_numpy(), as_now.iloc[period - 1:].to_numpy()
    ), "a warmed ATR value changed, so this edit altered sizing beyond the warm-up window"


def test_both_signal_bots_refuse_a_short_frame_identically():
    """RAMJET and ROCKETFUEL carry identical copies, and only one of them being fixed is worse
    than neither.

    A copy that still sizes off an unwarmed ATR would keep placing the too-tight stop while the
    other refuses, and the divergence would be invisible from the outside.

    This compares the two *behaviours* rather than their source text. An earlier version read
    `inspect.getsource` and asserted the string `min_periods=period` appeared - and a mutation
    that removed it from ROCKETFUEL **survived**, because `inspect.getsource` goes through
    `linecache`, which had already cached the file and kept serving the original text. A test that
    can be fooled by a cache is not a test, so this one calls both and compares what they return.
    """
    from signal_bots.rocketfuel.processor import RocketFuelSignalProcessor

    def frame_with(rows: int):
        frame = _sample_market_data(rows=rows).copy()
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        return frame

    bots = (RamJetSignalProcessor, RocketFuelSignalProcessor)

    for rows in (1, 3, 13):
        values = [bot._calculate_atr(frame_with(rows), 14)["atr"].iloc[-1] for bot in bots]
        for value in values:
            assert pd.isna(value), (
                f"a {rows}-bar frame still yields a usable ATR ({value!r}) in one of the two "
                "signal bots, so a short frame is being sized off an unwarmed ATR there"
            )

    # And on a warm frame they must still agree with each other, exactly. `equal_nan` because
    # the warm-up prefix is NaN in both, and NaN != NaN would make this fail for the wrong reason
    # - a comparison that is always false teaches you nothing.
    warm = [bot._calculate_atr(frame_with(40), 14)["atr"].to_numpy() for bot in bots]
    assert np.array_equal(warm[0], warm[1], equal_nan=True), (
        "the two bots disagree on a warm frame, so the copies have drifted beyond this change"
    )
    assert np.isfinite(warm[0][13:]).all(), (
        "no ATR is produced from bar `period - 1` onward, so the warm-up gate is wider than the "
        "strategies' own contract"
    )


def test_the_live_atr_smooths_with_span_and_the_strategies_with_wilders_alpha():
    """The alpha half of the inconsistency, pinned on both sides.

    Live: `frame["atr"] = frame["tr"].ewm(span=period, adjust=False).mean()` - alpha is
    `2/(p+1)`. Strategies: `alpha=1.0/period` with `min_periods=period` - Wilder's. On a 14-period
    that is 0.125 against 0.0667, so one reacts roughly twice as fast to a volatility change.

    The backlog's "the live stop sits about 10% closer to entry" is not reproduced by this
    measurement, and the sign is not even stable: on a growing-range series the live ATR came
    out ~8-10% *above* Wilder's at 14-60 bars and ~2% above by 300. A per-bar EWM of a stationary
    series has the same mean for any alpha, so there is no constant offset to find - which is
    what should have been checked before quoting a single-seed ratio as a finding.
    """
    processor = RamJetSignalProcessor(_config(period=14))

    # NOT `_sample_market_data`: it builds High = Close + 1.0 and Low = Close - 1.0, so every
    # bar has an identical true range and *any* EWM returns exactly 2.0. The shared fixture is
    # structurally incapable of distinguishing these two smoothings, which is a fair part of
    # why the inconsistency went unnoticed. A varying range is required to see alpha at all.
    rows = 300
    close = 100.0 + np.cumsum(np.random.default_rng(3).normal(0.0, 0.4, rows))
    width = np.linspace(0.5, 6.0, rows)
    market = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC"),
            "Open": close,
            "High": close + width,
            "Low": close - width,
            "Close": close,
            "Volume": np.full(rows, 10_000),
        }
    )

    tr = pd.concat(
        [
            market["High"] - market["Low"],
            (market["High"] - market["Close"].shift()).abs(),
            (market["Low"] - market["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # `_calculate_atr` consumes the prepared frame, not the raw market data.
    prepared = processor._prepare_market_data(market)
    live_last = float(processor._calculate_atr(prepared, 14)["atr"].iloc[-1])
    wilder_last = float(tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean().iloc[-1])
    assert live_last != pytest.approx(wilder_last, rel=1e-9), (
        "the live ATR and Wilder's now agree exactly, so the alpha half of item 5 has been "
        "resolved in code - update the backlog entry"
    )

    # The direction of the gap is data-dependent, so assert only that it is small and finite
    # rather than pinning a ratio that measurement already showed is noise.
    assert 0.5 < live_last / wilder_last < 2.0, (
        f"the two smoothings diverged by more than 2x ({live_last / wilder_last:.3f}); on a "
        "300-bar sample they should be close, so something else changed"
    )


def test_atr_has_no_readiness_marker_although_supertrend_does():
    """The fix for the warm-up half already exists in this file, for Supertrend only.

    Supertrend carries an explicit `supertrend_ready` column and refuses to report a direction
    before its warm-up, with a comment explaining that a pre-warmup `False` means "unknown"
    rather than "bearish" - a distinction the ATR path has no way to make. So the shape of the
    remedy is established in this codebase rather than invented: an `atr_ready` marker, or
    failing closed when the frame is shorter than `atr_period`.

    Pinned so the asymmetry is visible in the test file, and so that adding the marker without
    updating this test is a deliberate act.
    """
    processor = RamJetSignalProcessor(_config(period=14))
    market = _sample_market_data(rows=300)

    warmed = processor.process_signal(_signal(), market)
    assert warmed.metadata["supertrend_ready"] is True, (
        "Supertrend no longer reports readiness, so the precedent this test relies on is gone"
    )
    assert "atr_ready" not in warmed.metadata, (
        "the ATR path now carries an `atr_ready` marker, so item 5's warm-up half has been "
        "fixed - update this test and the backlog entry together"
    )
