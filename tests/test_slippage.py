"""Unit + engine tests for runner.slippage — fill-price slippage models (#130).

Decision (maintainer, 2026-11-21): slippage is applied to the FILL PRICE in
the adverse direction (never a separate cost line), scoped to the native
engine and signal replay. FX/CFD classes take the spread from bid/ask bar
data where available (MT5-tester-like) with a configured bps fallback;
stocks/crypto take a configurable fixed bps default per asset class. Every
run records the resolved model in ``meta.data["slippage"]``; ``slippage:
"none"`` is the explicit parity opt-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.slippage import (
    BarSpreadSlippage,
    FixedBpsSlippage,
    ZeroSlippage,
    resolve_slippage_model,
)


class TestResolveSlippageModel:
    def test_unclassified_asset_class_defaults_to_none(self) -> None:
        """Direct engine use (no asset class) records an explicit none."""
        model = resolve_slippage_model({}, asset_class="")
        assert isinstance(model, ZeroSlippage)
        assert model.describe()["model"] == "none"

    def test_equity_defaults_to_fixed_bps(self) -> None:
        model = resolve_slippage_model({}, asset_class="equities")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(5.0)

    def test_crypto_defaults_to_fixed_bps(self) -> None:
        model = resolve_slippage_model({}, asset_class="crypto")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(10.0)

    def test_forex_defaults_to_bar_spread(self) -> None:
        model = resolve_slippage_model({}, asset_class="forex")
        assert isinstance(model, BarSpreadSlippage)

    def test_explicit_none_is_parity_opt_out(self) -> None:
        for spelling in ("none", "zero", "off", "parity"):
            model = resolve_slippage_model({"slippage": spelling}, asset_class="equities")
            assert isinstance(model, ZeroSlippage), spelling

    def test_numeric_slippage_is_fixed_bps(self) -> None:
        model = resolve_slippage_model({"slippage": 3.5}, asset_class="equities")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(3.5)

    def test_slippage_bps_key_builds_fixed_model(self) -> None:
        model = resolve_slippage_model({"slippage_bps": 2.0}, asset_class="forex")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(2.0)

    def test_spread_string_builds_bar_spread_model(self) -> None:
        model = resolve_slippage_model({"slippage": "spread"}, asset_class="equities")
        assert isinstance(model, BarSpreadSlippage)

    def test_bps_string_requires_or_uses_default(self) -> None:
        model = resolve_slippage_model(
            {"slippage": "bps", "slippage_bps": 4.0}, asset_class="equities"
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(4.0)
        with pytest.raises(ValueError, match="slippage_bps"):
            resolve_slippage_model({"slippage": "bps"}, asset_class="")

    def test_unknown_spelling_rejected(self) -> None:
        with pytest.raises(ValueError, match="slippage"):
            resolve_slippage_model({"slippage": "bogus"}, asset_class="equities")

    def test_explicit_model_passthrough(self) -> None:
        sentinel = FixedBpsSlippage(bps=1.0)
        assert resolve_slippage_model({"slippage_model": sentinel}) is sentinel

    def test_negative_bps_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            FixedBpsSlippage(bps=-1.0)

    def test_slippage_defaults_override_table(self) -> None:
        model = resolve_slippage_model(
            {"slippage_defaults": {"equities": 12.5}}, asset_class="equities"
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(12.5)


class TestSlipPrice:
    def test_fixed_bps_adverse_direction(self) -> None:
        model = FixedBpsSlippage(bps=10.0)  # 0.1%
        assert model.slip_price(100.0, "buy") == pytest.approx(100.1)
        assert model.slip_price(100.0, "sell") == pytest.approx(99.9)

    def test_zero_is_identity(self) -> None:
        model = ZeroSlippage()
        assert model.slip_price(123.45, "buy") == 123.45
        assert model.slip_price(123.45, "sell") == 123.45

    def test_bar_spread_uses_bid_ask_columns(self) -> None:
        model = BarSpreadSlippage(default_bps=50.0)
        bar = pd.Series({"open": 100.0, "close": 100.0, "ask_close": 100.4})
        # ask - bid = 0.4 price units beats the 50bps fallback (0.5).
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.4)
        assert model.slip_price(100.0, "sell", bar) == pytest.approx(99.6)

    def test_bar_spread_falls_back_to_bps_without_ask(self) -> None:
        model = BarSpreadSlippage(default_bps=10.0)
        bar = pd.Series({"open": 100.0, "close": 100.0})
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.1)
        assert model.slip_price(100.0, "sell", bar) == pytest.approx(99.9)

    def test_bar_spread_ignores_nonpositive_diffs(self) -> None:
        # Ask-side canonical data (ask_close == close) must not invent a
        # negative/zero spread; the bps fallback applies instead.
        model = BarSpreadSlippage(default_bps=10.0)
        bar = pd.Series({"open": 100.0, "close": 100.0, "ask_close": 100.0})
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.1)


def _engine_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=30, freq="h", tz="UTC"),
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.5] * 30,
            "volume": [1000.0] * 30,
        }
    )


def _engine_strategy() -> Any:
    def strategy_fn(df, params):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[10] = True
        exits.iloc[20] = True
        return entries, exits, {}, []

    return strategy_fn


def _engine_config(parameters: dict, **overrides) -> Any:
    from runner.engines.base import BacktestConfig

    payload = {
        "strategy_name": "slip-test",
        "symbols": ["TEST"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "initial_capital": 100_000,
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "parameters": {"fees": "zero", **parameters},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


class TestNativeEngineFillPriceSlippage:
    def test_long_entry_and_exit_sit_adversely(self) -> None:
        from runner.engine import TradeEngine

        engine = TradeEngine(
            _engine_config({"slippage_bps": 100.0}),  # 1%
            symbol="TEST",
        )
        result = engine.run(_engine_frame(), _engine_strategy())
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Market entry fills next bar open (100.0); +1% adverse => 101.0.
        assert trade.entry_price == pytest.approx(101.0)
        # Signal exit fills at the signal bar's close (100.5); -1% => 99.495.
        assert trade.exits[0].price == pytest.approx(100.5 * 0.99)
        # Slippage flows through PnL, not a separate cost line.
        assert trade.fees == 0.0
        qty = trade.quantity
        expected_pnl = (trade.exits[0].price - trade.entry_price) * qty
        assert trade.pnl == pytest.approx(expected_pnl, abs=0.02)

    def test_none_slippage_preserves_parity_prices(self) -> None:
        from runner.engine import TradeEngine

        engine = TradeEngine(
            _engine_config({"slippage": "none"}),
            symbol="TEST",
        )
        result = engine.run(_engine_frame(), _engine_strategy())
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(100.0)
        assert trade.exits[0].price == pytest.approx(100.5)

    def test_short_direction_slips_the_other_way(self) -> None:
        from runner.engine import TradeEngine

        def short_strategy(df, params):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[10] = True
            exits.iloc[20] = True
            direction = pd.Series("SHORT", index=df.index)
            return entries, exits, {"direction": direction}, []

        engine = TradeEngine(
            _engine_config({"slippage_bps": 100.0}),
            symbol="TEST",
        )
        result = engine.run(_engine_frame(), short_strategy)
        trade = result.trades[0]
        # SHORT entry sells: 100.0 * 0.99 = 99.0; exit buys: 100.5 * 1.01.
        assert trade.entry_price == pytest.approx(99.0)
        assert trade.exits[0].price == pytest.approx(100.5 * 1.01)


class TestNativeAdapterMetadata:
    def test_run_records_slippage_model_in_meta(self, tmp_path: Path) -> None:
        from runner.engines.base import BacktestConfig
        from runner.engines.native_adapter import NativeAdapter
        from tests.helpers import write_ohlcv

        frame = _engine_frame()
        write_ohlcv(tmp_path, "SLIP", frame, asset_class="equities")
        strategy_path = tmp_path / "strat.py"
        strategy_path.write_text(
            "import pandas as pd\n"
            "def generate_signals(df, params):\n"
            "    n = len(df)\n"
            "    entries = pd.Series([i == 10 for i in range(n)], index=df.index)\n"
            "    exits = pd.Series([i == 20 for i in range(n)], index=df.index)\n"
            "    return entries, exits, {}, []\n"
        )
        config = BacktestConfig(
            strategy_name="slip",
            symbols=["SLIP"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000,
            risk_per_trade_pct=0.01,
            modeling_resolution="main_timeframe",
            data_dir=str(tmp_path),
            parameters={"fees": "zero"},
        )
        result = NativeAdapter().run(config, str(strategy_path))
        slippage_meta = result.meta.data["slippage"]
        models = slippage_meta["models"]
        assert set(models) == {"SLIP"}
        assert models["SLIP"]["model"] == "fixed_bps"
        assert models["SLIP"]["asset_class"] == "equities"
        # Classified equity run defaults to 5bps: entry 100 -> 100.05.
        assert result.trades[0].entry_price == pytest.approx(100.05)


class TestSignalReplaySlippage:
    def _adapter(self, **kwargs):
        from runner.engines.signal_replay_adapter import SignalReplayAdapter

        return SignalReplayAdapter(**kwargs)

    def _signal(self):
        from runner.engines.signal_replay_adapter import ReplaySignal

        return ReplaySignal(
            timestamp=pd.Timestamp("2024-01-01 10:00", tz="UTC"),
            symbol="AAA",
            action="buy",
            entry_price=100.0,
            # Distant stop: the risk engine's fallback stop would otherwise
            # fire inside the flat 99-101 bar range before end-of-data.
            stop_loss=50.0,
        )

    def test_entry_and_exit_fill_price_slipped(self) -> None:
        adapter = self._adapter(
            fee_params={"fees": "zero"}, slippage_params={"slippage_bps": 100.0}
        )
        frame = _engine_frame()
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": frame},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
        )
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Fill bar is the first bar at/after the signal timestamp (10:00 bar,
        # close 100.5); LONG buy slips up 1%.
        assert trade.entry_price == pytest.approx(100.5 * 1.01)
        # End-of-data exit sells: last bar close 100.5, -1%.
        assert trade.exits[-1].price == pytest.approx(100.5 * 0.99)
        assert result.meta.data["slippage"]["models"]["AAA"]["model"] == "fixed_bps"

    def test_none_slippage_preserves_replay_parity(self) -> None:
        adapter = self._adapter(
            fee_params={"fees": "zero"}, slippage_params={"slippage": "none"}
        )
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": _engine_frame()},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
        )
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(100.5)
        assert trade.exits[-1].price == pytest.approx(100.5)
