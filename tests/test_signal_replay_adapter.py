from __future__ import annotations

import pandas as pd
import pytest

from risk.config import RiskConfig
from runner.engines.signal_replay_adapter import (
    ReplaySignal,
    SignalReplayAdapter,
    SignalReplayEngine,
)


def test_signal_replay_adapter_updates_trailing_stop_and_exits() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1min"),
            "open": [100.0, 100.5, 102.2, 101.0],
            "high": [100.8, 103.0, 102.5, 101.3],
            "low": [99.8, 100.2, 101.0, 100.7],
            "close": [100.0, 103.0, 101.2, 101.0],
            "volume": [1000, 1000, 1000, 1000],
        }
    )
    signal = ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 140.0,
            "timeframe": "1m",
            "source": "csv:signals.csv",
            "metadata": {
                "trailing_sl_config": {"mode": "percent", "percent": 0.01},
            },
        },
        default_timeframe="1m",
    )

    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, slippage_params={"slippage": "none"},
        risk_config=RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        ),
        initial_capital=100000.0,
        default_timeframe="1m",
    )
    result = adapter.run(
        signals=[signal],
        ohlcv_by_symbol={"AAPL": frame},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.meta["signal_source"] == "csv:signals.csv"
    assert trade.meta["signal_stop_loss"] == 98.0
    assert trade.meta["trailing_stop_updates"] >= 1
    assert trade.meta["fill_delay_seconds"] == 0


def _trailing_replay_signal(
    mode_config: dict, timestamp: str = "2026-01-01T00:00:00Z"
) -> ReplaySignal:
    return ReplaySignal.from_payload(
        {
            "timestamp": timestamp,
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 140.0,
            "timeframe": "1m",
            "source": "csv:signals.csv",
            "metadata": {"trailing_sl_config": mode_config},
        },
        default_timeframe="1m",
    )


def _one_bar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1min"),
            "open": [100.0, 100.5, 102.2, 101.0],
            "high": [100.8, 103.0, 102.5, 101.3],
            "low": [99.8, 100.2, 101.0, 100.7],
            "close": [100.0, 103.0, 101.2, 101.0],
            "volume": [1000, 1000, 1000, 1000],
        }
    )


def _run_replay(signals: list[ReplaySignal]):
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, slippage_params={"slippage": "none"}, default_timeframe="1m")
    return adapter.run(
        signals=signals,
        ohlcv_by_symbol={"AAPL": _one_bar_frame()},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )


def test_replay_executes_supertrend_flip() -> None:
    """#168 (decided change of the #150 rejection): replay now executes
    supertrend_flip instead of rejecting it. The workflow itself is covered
    in ``tests/test_replay_supertrend_flip.py``."""
    signal = ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 102.0,
            "timeframe": "1m",
            "metadata": {
                "trailing_sl_config": {
                    "mode": "supertrend_flip",
                    "breakeven_after_tp1": True,
                    "supertrend_period": 2,
                    "supertrend_multiplier": 1.0,
                },
                "tp1_price": 102.0,
                "tp1_exit_percent": 0.5,
            },
        },
        default_timeframe="1m",
    )
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, slippage_params={"slippage": "none"}, default_timeframe="1m")
    result = adapter.run(
        signals=[signal],
        ohlcv_by_symbol={"AAPL": _one_bar_frame()},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[0].reason == "tp1"
    assert trade.exits[0].price == 102.0
    assert trade.meta["supertrend_flip"]["tp1_time"] is not None


def test_replay_rejects_supertrend_flip_without_its_parameters() -> None:
    """A bare {"mode": "supertrend_flip"} names the workflow without its
    parameters; it is refused, never run with invented defaults."""
    signals = [_trailing_replay_signal({"mode": "supertrend_flip"})]
    with pytest.raises(ValueError, match="supertrend_flip"):
        _run_replay(signals)


def test_replay_rejects_a_signal_at_signal_time_before_the_engine_backstop() -> None:
    """Review survivor: the replay gate itself (not the engine backstop) must refuse.

    The engine backstop raises a different message, so `match="signal replay"`
    only passes if the signal-time gate in the adapter rejected the signal.
    """
    signals = [
        _trailing_replay_signal({"mode": "none"}),
        _trailing_replay_signal({"mode": "supertrend_flip"}, timestamp="2026-01-01T00:01:00Z"),
    ]
    with pytest.raises(ValueError, match="signal replay"):
        _run_replay(signals)


def test_replay_rejects_unknown_trailing_mode() -> None:
    signals = [_trailing_replay_signal({"mode": "martingale_trail"})]
    with pytest.raises(ValueError, match="Unknown trailing stop mode"):
        _run_replay(signals)


def test_replay_mode_none_trades_with_initial_stop_only() -> None:
    """#150: mode 'none' is replay-supported: initial stop, no trailing."""
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, slippage_params={"slippage": "none"}, default_timeframe="1m")
    result = adapter.run(
        signals=[_trailing_replay_signal({"mode": "none"})],
        ohlcv_by_symbol={"AAPL": _one_bar_frame()},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )
    assert len(result.trades) == 1
    # The initial stop is carried untouched (no bar in this frame hits it),
    # proving mode 'none' neither trails nor errors.
    assert result.trades[0].meta["final_stop_loss"] == pytest.approx(98.0)
    assert result.trades[0].meta["trailing_stop_updates"] == 0


# ─── The public entry point: SignalReplayEngine.run_backtest ──────────────────
#
# ``run_backtest`` is what the Discord replay command actually calls: it turns
# signal payloads into ``ReplaySignal``\ s, loads (or downloads) the OHLCV
# shards, runs the adapter and tags the result. None of it ran before these
# tests - a regression in the payload parsing, the data_dir layout or the
# download fallback would have shipped silently.


def _buy_signal_payload(**overrides):
    payload = {
        "timestamp": "2026-01-01T00:00:00Z",
        "symbol": "AAPL",
        "action": "buy",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 140.0,
        "timeframe": "1m",
        "source": "csv:signals.csv",
        "metadata": {"trailing_sl_config": {"mode": "none"}},
    }
    payload.update(overrides)
    return payload


def _write_ohlcv_shard(data_dir, symbol: str = "AAPL", timeframe: str = "1m") -> None:
    """Drop a fixture frame where ``_load_ohlcv`` looks for it."""
    target = data_dir / "ohlcv" / symbol / f"{timeframe}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    _one_bar_frame().to_parquet(target, index=False)


def _replay_engine(data_dir) -> SignalReplayEngine:
    return SignalReplayEngine(
        data_dir=data_dir,
        default_timeframe="1m",
        fee_params={"fees": "zero"},
        slippage_params={"slippage": "none"},
    )


def _run_backtest(engine: SignalReplayEngine, **kwargs):
    params = {
        "signals": [_buy_signal_payload()],
        "signal_processor": None,
        "symbols": ["AAPL"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-01",
        "initial_capital": 100_000.0,
        "timeframe": "1m",
        "auto_download": False,
    }
    params.update(kwargs)
    return engine.run_backtest(**params)


def test_run_backtest_replays_signals_from_a_fixture_parquet(tmp_path) -> None:
    """End to end: payload dicts in, a tagged BacktestResult out."""
    _write_ohlcv_shard(tmp_path)
    engine = _replay_engine(tmp_path)

    result = _run_backtest(engine)

    # The only bar after the signal never reaches the stop, so the position is
    # closed at end-of-data: one trade, entry at the signal bar's close.
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.symbol == "AAPL"
    assert trade.meta["signal_source"] == "csv:signals.csv"
    assert result.meta.engine == "signal-replay"
    assert {"signal_replay", "signal-replay"} <= set(result.meta.tags or [])
    assert result.meta.data["symbols"] == ["AAPL"]
    assert result.meta.data["timeframes"] == ["1m"]


def test_run_backtest_refuses_a_symbol_with_no_ohlcv_and_no_download(tmp_path) -> None:
    engine = _replay_engine(tmp_path)

    with pytest.raises(ValueError, match="Missing OHLCV for AAPL"):
        _run_backtest(engine)


def test_run_backtest_auto_download_fetches_the_missing_symbol(tmp_path, monkeypatch) -> None:
    """``auto_download`` must actually invoke the downloader, then re-load."""
    engine = _replay_engine(tmp_path)
    downloads = []

    def fake_download(*, symbol, timeframe, start, end) -> None:
        downloads.append((symbol, timeframe, start, end))
        _write_ohlcv_shard(tmp_path, symbol=symbol, timeframe=timeframe)

    monkeypatch.setattr(engine, "_download_yfinance", fake_download)

    result = _run_backtest(engine, auto_download=True)

    assert downloads == [("AAPL", "1m", "2026-01-01", "2026-01-01")]
    assert len(result.trades) == 1


def test_run_backtest_still_refuses_when_the_download_produced_nothing(
    tmp_path, monkeypatch
) -> None:
    """A downloader that leaves no parquet must not turn into an empty result."""
    engine = _replay_engine(tmp_path)
    monkeypatch.setattr(engine, "_download_yfinance", lambda **_: None)

    with pytest.raises(ValueError, match="Missing OHLCV for AAPL"):
        _run_backtest(engine, auto_download=True)


def test_run_backtest_rejects_empty_signals_and_symbols(tmp_path) -> None:
    engine = _replay_engine(tmp_path)

    with pytest.raises(ValueError, match="signals must not be empty"):
        _run_backtest(engine, signals=[])
    with pytest.raises(ValueError, match="symbols must not be empty"):
        _run_backtest(engine, symbols=[" "])


def test_build_risk_signal_maps_direction_levels_and_metadata() -> None:
    """The risk signal is what the sizing engine sees: direction, fill price,
    the signal's own levels and the live position count."""
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, slippage_params={"slippage": "none"}, default_timeframe="1m")
    signal = ReplaySignal(
        timestamp=pd.Timestamp("2026-01-01T00:00:00Z"),
        symbol="aapl",
        action="sell",
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=95.0,
        timeframe="",
        source="csv:signals.csv",
        metadata={"note": "replayed"},
    )
    open_positions = {"AAPL": object(), "MSFT": object()}

    risk_signal = adapter._build_risk_signal(
        effective_signal=signal,
        target_direction="SHORT",
        fill_price=99.5,
        normalized_timeframe="1m",
        open_positions=open_positions,
    )

    assert risk_signal.action == "sell"
    assert risk_signal.symbol == "AAPL"
    # The fill price is the bar's close, not the signal's declared entry.
    assert risk_signal.entry_price == 99.5
    assert risk_signal.stop_loss == 102.0
    assert risk_signal.take_profit == 95.0
    # An empty signal timeframe falls back to the run's normalized one.
    assert risk_signal.timeframe == "1m"
    assert risk_signal.metadata["note"] == "replayed"
    assert risk_signal.metadata["current_positions"] == 2

    long_signal = adapter._build_risk_signal(
        effective_signal=signal,
        target_direction="LONG",
        fill_price=99.5,
        normalized_timeframe="1m",
        open_positions={},
    )
    assert long_signal.action == "buy"
    assert long_signal.metadata["current_positions"] == 0
