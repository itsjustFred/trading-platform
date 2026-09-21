"""Cross-engine contract tests (A-L9): native vs vectorbt parity surface.

Pins the parity regressions from A-H4 (warmup_start, signal_exit_fill,
spread/ask) and A-H2 (entry_stop), plus the unified D4 exit convention
(Fill-zum-Trigger-Close, A-M1) and the D5 shared-equity replay (A-M4):

- signal exits fill at the trigger bar's close in BOTH engines;
- LONG market entries pay the spread column in BOTH engines;
- warmup bars create no trades in BOTH engines;
- inverted-stop drops are counted and reported (A-M7);
- multi-symbol runs share one portfolio equity with summed exposure (A-M4).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.engines.native_adapter import NativeAdapter
from tests.helpers import write_ohlcv as _write_ohlcv



def _with_zero_fees(payload: dict) -> dict:
    """#84: explicit no-cost declaration for engine fixtures."""
    merged = dict(payload)
    params = dict(merged.get("parameters") or {})
    params.setdefault("fees", "zero")
    merged["parameters"] = params
    return merged

def _frame(*, spread: float = 0.0) -> pd.DataFrame:
    closes = [100.0 + i * 1.0 for i in range(12)]
    data: dict[str, list] = {
        "timestamp": pd.date_range("2024-01-01", periods=12, freq="h", tz="UTC"),
        "open": list(closes),
        "high": [c + 0.25 for c in closes],
        "low": [c - 0.25 for c in closes],
        "close": closes,
        "volume": [1000.0] * 12,
    }
    if spread:
        data["spread_price"] = [spread] * 12
    return pd.DataFrame(data)


def _strategy_text(*, entry_bar: int = 2, exit_bar: int = 7, spread_col: bool = False) -> str:
    return f'''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry_bar}] = True
    exits.iloc[{exit_bar}] = True
    indicators = {{"direction": pd.Series(["LONG"] * n, index=df.index)}}
    indicators["exit_reason"] = pd.Series([None] * n, index=df.index, dtype=object)
    indicators["exit_reason"].iloc[{exit_bar}] = "signal"
    return entries, exits, indicators, []
'''


def _write_strategy(tmp_path: Path, name: str, text: str) -> str:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(exist_ok=True)
    path = strategy_dir / f"{name}.py"
    path.write_text(text)
    return str(path)


def _native_result(frame: pd.DataFrame, strategy_fn, **config_kwargs):
    payload = {
        "strategy_name": "contract",
        "symbols": ["CEC"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
    }
    payload.update(config_kwargs)
    config = BacktestConfig(**_with_zero_fees(payload))
    engine = TradeEngine(config, symbol="CEC")
    return engine.run(frame, strategy_fn)


def _vectorbt_result(tmp_path: Path, frame: pd.DataFrame, strategy_text: str, **config_kwargs):
    _write_ohlcv(tmp_path, "CEC", frame)
    strategy_path = _write_strategy(tmp_path, "contract_strat", strategy_text)
    payload = {
        "strategy_name": "contract",
        "symbols": ["CEC"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(config_kwargs)
    return VectorbtAdapter().run(BacktestConfig(**_with_zero_fees(payload)), strategy_path)


def _load_strategy_fn(strategy_text: str, tmp_path: Path, name: str):
    import importlib.util

    path = _write_strategy(tmp_path, name, strategy_text)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_signals


class TestSignalExitConvention:
    """D4 (A-M1): signal exits fill at the trigger bar's close, both engines."""

    def test_exit_price_equals_trigger_close_on_both_engines(self, tmp_path: Path):
        frame = _frame()
        text = _strategy_text(entry_bar=2, exit_bar=7)
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_exit")

        native = _native_result(frame, strategy_fn)
        vectorbt = _vectorbt_result(tmp_path, frame, text)

        assert len(native.trades) == 1
        assert len(vectorbt.trades) == 1
        expected_entry = float(frame["open"].iloc[3])  # next-open after bar 2
        expected_exit = float(frame["close"].iloc[7])  # trigger close
        for result in (native, vectorbt):
            trade = result.trades[0]
            assert trade.entry_price == pytest.approx(expected_entry)
            assert trade.exits[0].price == pytest.approx(expected_exit)
            assert trade.exits[0].reason == "signal"
        # Exit timestamps agree (same trigger bar, format-insensitive).
        native_ts = pd.to_datetime(native.trades[0].exits[0].time, utc=True)
        vbt_ts = pd.to_datetime(vectorbt.trades[0].exits[0].time, utc=True)
        assert native_ts == vbt_ts

    def test_long_market_entry_pays_spread_on_both_engines(self, tmp_path: Path):
        frame = _frame(spread=0.5)
        text = _strategy_text(entry_bar=2, exit_bar=7)
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_spread")
        params = {"spread_price_col": "spread_price"}

        native = _native_result(frame, strategy_fn, parameters=params)
        vectorbt = _vectorbt_result(tmp_path, frame, text, parameters=params)

        expected_entry = float(frame["open"].iloc[3]) + 0.5
        assert native.trades[0].entry_price == pytest.approx(expected_entry)
        assert vectorbt.trades[0].entry_price == pytest.approx(expected_entry)

    def test_warmup_bars_create_no_trades_on_both_engines(self, tmp_path: Path):
        # Entry/exit on bars 1->2 (warmup) must be ignored; bars 6->8 trade.
        text = """
import pandas as pd


def generate_signals(df, params):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[2] = True
    entries.iloc[6] = True
    exits.iloc[8] = True
    indicators = {"direction": pd.Series(["LONG"] * len(df), index=df.index)}
    return entries, exits, indicators, []
"""
        frame = _frame()
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_warmup")
        warmup = {
            "warmup_start": "2024-01-01 00:00",
            "start_date": "2024-01-01 05:00",
            "end_date": "2024-01-02",
        }
        native = _native_result(frame, strategy_fn, **warmup)
        vectorbt = _vectorbt_result(tmp_path, frame, text, **warmup)

        assert len(native.trades) == 1
        assert len(vectorbt.trades) == 1
        # Entry signal bar 6 fills at bar 7 open; exit signal bar 8 at close.
        assert native.trades[0].entry_price == pytest.approx(float(frame["open"].iloc[7]))
        assert vectorbt.trades[0].entry_price == pytest.approx(float(frame["open"].iloc[7]))
        assert native.trades[0].exits[0].price == pytest.approx(float(frame["close"].iloc[8]))
        assert vectorbt.trades[0].exits[0].price == pytest.approx(float(frame["close"].iloc[8]))

    def test_warmup_without_start_date_fails_loudly(self, tmp_path: Path):
        text = _strategy_text()
        frame = _frame()
        strategy_fn = _load_strategy_fn(text, tmp_path, "contract_warmup_fail")
        with pytest.raises(ValueError, match="warmup_start"):
            _native_result(
                frame, strategy_fn, warmup_start="2024-01-01 00:00", start_date=""
            )

    def test_signal_exit_fill_rejects_unknown_values(self, tmp_path: Path):
        """D4: the fill-convention switch is load-bearing — a typo must fail
        loudly instead of silently falling through to close-fill."""
        with pytest.raises(ValueError, match="signal_exit_fill"):
            BacktestConfig(parameters={"fees": "zero"}, 
                strategy_name="contract",
                symbols=["CEC"],
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-01-02",
                initial_capital=100_000.0,
                signal_exit_fill="clos",
            )

    def test_signal_exit_fill_normalizes_case(self, tmp_path: Path):
        config = BacktestConfig(parameters={"fees": "zero"}, 
            strategy_name="contract",
            symbols=["CEC"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            signal_exit_fill="Close",
        )
        assert config.signal_exit_fill == "close"


class TestNativeWarmupLoad:
    """A-H4: NativeAdapter loads from warmup_start so indicators are warm."""

    def test_warmup_start_extends_loaded_window(self, tmp_path: Path):
        _write_ohlcv(tmp_path, "CEC", _frame())
        strategy_path = _write_strategy(tmp_path, "contract_warmup_load", _strategy_text())
        config = BacktestConfig(parameters={"fees": "zero"}, 
            strategy_name="contract",
            symbols=["CEC"],
            timeframe="1h",
            start_date="2024-01-01 05:00",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            warmup_start="2024-01-01 00:00",
            data_dir=str(tmp_path),
        )
        result = NativeAdapter().run(config, strategy_path)
        # 12 bars loaded (00:00..11:00), not just the 7 since start_date.
        assert result.meta.total_bars == 12


_INVERTED_TEXT = """
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[2] = True
    exits.iloc[7] = True
    indicators = {
        "direction": pd.Series(["LONG"] * n, index=df.index),
        "sl_price": pd.Series([float("nan")] * n, index=df.index),
    }
    indicators["sl_price"].iloc[2] = 500.0  # wrong side of a ~102 entry
    return entries, exits, indicators, []
"""


class TestInvertedStopReport:
    """A-M7: drops are counted and reported, not just warn-once logged."""

    def test_inverted_stop_drop_count_reported_in_meta(self, tmp_path: Path):
        result = _vectorbt_result(
            tmp_path,
            _frame(),
            _INVERTED_TEXT,
            parameters={},
        )
        assert result.trades == []
        assert result.meta.data["inverted_stop_drops"] == 1


class TestSharedEquity:
    """A-M4 (D5): multi-symbol runs share one portfolio equity."""

    def _two_symbol_env(self, tmp_path: Path):
        for symbol in ("SH1", "SH2"):
            _write_ohlcv(tmp_path, symbol, _frame())
        return _write_strategy(tmp_path, "shared_strat", _strategy_text())

    def test_vectorbt_multi_symbol_replays_shared_equity(self, tmp_path: Path):
        strategy_path = self._two_symbol_env(tmp_path)
        config = BacktestConfig(
            strategy_name="contract",
            symbols=["SH1", "SH2"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero"},
        )
        result = VectorbtAdapter().run(config, strategy_path)
        assert len(result.trades) == 2
        summary = result.meta.data["shared_equity"]
        assert summary["shared_equity"] is True
        assert summary["symbols_traded"] == ["SH1", "SH2"]
        for trade in result.trades:
            assert "portfolio" in trade.meta
            # Simultaneous entries: nothing exited before either entry, so
            # both size from the full starting capital (no phantom scaling).
            assert trade.meta["portfolio"]["equity_at_entry"] == pytest.approx(100_000.0)
        # Exposure is SUMMED across symbols while both are open (D5) — the
        # peak exceeds either trade's notional alone.
        notionals = [t.entry_price * t.quantity for t in result.trades]
        assert summary["peak_open_notional_usd"] == pytest.approx(
            sum(notionals), rel=1e-4
        )
        final = result.equity_curve[-1].equity
        expected = 100_000.0 + sum(t.net_pnl if t.net_pnl is not None else t.pnl for t in result.trades)
        assert final == pytest.approx(expected, abs=0.02)

    def test_native_multi_symbol_replays_shared_equity(self, tmp_path: Path):
        strategy_path = self._two_symbol_env(tmp_path)
        config = BacktestConfig(
            strategy_name="contract",
            symbols=["SH1", "SH2"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero"},
        )
        result = NativeAdapter().run(config, strategy_path)
        assert len(result.trades) == 2
        assert result.meta.data["shared_equity"]["shared_equity"] is True
        for trade in result.trades:
            assert "portfolio" in trade.meta

    def test_single_symbol_runs_skip_replay(self, tmp_path: Path):
        result = _vectorbt_result(tmp_path, _frame(), _strategy_text())
        assert result.meta.data["shared_equity"] == {}
        assert "portfolio" not in result.trades[0].meta


def _replay_trade(trade_id: int, symbol: str, entry: str, exit: str,
                  entry_price: float, exit_price: float, qty: float,
                  equity_at_entry: float) -> "Trade":
    from shared.models import Trade, TradeExit

    direction = "LONG"
    pnl = (exit_price - entry_price) * qty
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry,
        entry_price=entry_price,
        quantity=qty,
        exits=[TradeExit(time=exit, price=exit_price, quantity=qty, pnl=pnl)],
        pnl=pnl,
        meta={"risk_math": {"equity_at_entry": equity_at_entry}},
    )


class TestSharedEquityReplayMath:
    """Unit tests for the D5 replay: chronological rescaling + exposure sum."""

    def test_sequential_trades_compound_on_shared_equity(self):
        from runner.engines._shared import replay_shared_equity

        # Both sized from 100k full capital: 100 @ 100 -> 110 (+1000).
        t1 = _replay_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
                           100.0, 110.0, 100.0, 100_000.0)
        # Second trade sized from 100k as well: 50 @ 200 -> 204 (+200).
        t2 = _replay_trade(2, "B", "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z",
                           200.0, 204.0, 50.0, 100_000.0)
        replayed, curve, summary = replay_shared_equity([t1, t2], 100_000.0)
        # t1 unchanged (first on shared equity); t2 rescales by 101/100.
        assert replayed[0].quantity == pytest.approx(100.0)
        assert replayed[1].quantity == pytest.approx(50.0 * 1.01)
        assert replayed[1].pnl == pytest.approx(200.0 * 1.01)
        assert curve[-1].equity == pytest.approx(100_000.0 + 1000.0 + 202.0)
        assert summary["shared_equity"] is True

    def test_overlapping_trades_sum_open_exposure(self):
        from runner.engines._shared import replay_shared_equity

        t1 = _replay_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-05T00:00:00Z",
                           100.0, 100.0, 100.0, 100_000.0)
        t2 = _replay_trade(2, "B", "2024-01-02T00:00:00Z", "2024-01-06T00:00:00Z",
                           100.0, 100.0, 100.0, 100_000.0)
        replayed, _, summary = replay_shared_equity([t1, t2], 100_000.0)
        # t2 enters while t1 is still open: one open position recorded.
        by_id = {t.id: t for t in replayed}
        assert by_id[2].meta["portfolio"]["open_positions"] == 1
        assert summary["peak_open_notional_usd"] == pytest.approx(20_000.0)

    def test_replay_scales_fees_with_quantity(self):
        from shared.models import Trade, TradeExit

        from runner.engines._shared import replay_shared_equity

        def _fee_trade(trade_id, symbol, entry, exit, qty, fees):
            pnl = (110.0 - 100.0) * qty
            return Trade(
                id=trade_id,
                symbol=symbol,
                direction="LONG",
                entry_time=entry,
                entry_price=100.0,
                quantity=qty,
                exits=[TradeExit(time=exit, price=110.0, quantity=qty, pnl=pnl)],
                pnl=pnl,
                fees=fees,
                net_pnl=pnl - fees,
                meta={"risk_math": {"equity_at_entry": 100_000.0}},
            )

        # Sequential: t2 resizes by 101/100 on shared equity — fees are
        # per-share costs and must scale with the quantity.
        t1 = _fee_trade(1, "A", "2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z",
                        100.0, 50.0)
        t2 = _fee_trade(2, "B", "2024-01-03T00:00:00Z", "2024-01-04T00:00:00Z",
                        100.0, 50.0)
        replayed, _, _ = replay_shared_equity([t1, t2], 100_000.0)
        by_id = {t.id: t for t in replayed}
        assert by_id[1].fees == pytest.approx(50.0)
        # t1 nets +950, so t2 rescales by 100950/100000 — fees scale with it.
        assert by_id[2].quantity == pytest.approx(100.0 * 1.0095)
        assert by_id[2].fees == pytest.approx(50.0 * 1.0095, abs=0.01)
        assert by_id[2].net_pnl == pytest.approx(by_id[2].pnl - by_id[2].fees)

    def test_replay_processes_entries_in_entry_order(self):
        from runner.engines._shared import replay_shared_equity

        # D exits (t1.5, +1000) before B enters (t2) but AFTER A enters
        # (t1): entry-order replay sizes A from the still-unreleased shared
        # equity. Exit-order processing would release D's profit first and
        # over-size A.
        d = _replay_trade(3, "D", "2024-01-01T00:30:00Z", "2024-01-01T01:30:00Z",
                          100.0, 101.0, 1000.0, 100_000.0)
        a = _replay_trade(1, "A", "2024-01-01T01:00:00Z", "2024-01-05T00:00:00Z",
                          100.0, 100.0, 100.0, 100_000.0)
        b = _replay_trade(2, "B", "2024-01-01T02:00:00Z", "2024-01-03T00:00:00Z",
                          100.0, 100.0, 100.0, 100_000.0)
        replayed, _, _ = replay_shared_equity([a, b, d], 100_000.0)
        by_id = {t.id: t for t in replayed}
        assert by_id[1].quantity == pytest.approx(100.0)
