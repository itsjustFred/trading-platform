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

    def test_auto_with_slippage_bps_is_rejected(self) -> None:
        """``auto`` + ``slippage_bps`` must not silently drop the declared bps."""
        for spelling in ("auto", "default", ""):
            with pytest.raises(ValueError, match="slippage_bps"):
                resolve_slippage_model(
                    {"slippage": spelling, "slippage_bps": 7.0},
                    asset_class="equities",
                )

    def test_auto_without_slippage_key_and_bps_still_fixed(self) -> None:
        """Absent ``slippage`` + ``slippage_bps`` is an explicit fixed model,
        not the auto path — the bps is honoured, not rejected."""
        model = resolve_slippage_model({"slippage_bps": 7.0}, asset_class="forex")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(7.0)

    def test_spread_without_class_or_bps_is_rejected(self) -> None:
        """``slippage='spread'`` may not fall back to a hard-coded 5bps."""
        with pytest.raises(ValueError, match="slippage_bps|slippage_default_bps"):
            resolve_slippage_model({"slippage": "spread"}, asset_class="")
        # ... but an explicit fallback or a classified asset class is fine.
        model = resolve_slippage_model(
            {"slippage": "spread", "slippage_default_bps": 3.0}, asset_class=""
        )
        assert isinstance(model, BarSpreadSlippage)
        assert model.default_bps == pytest.approx(3.0)
        model = resolve_slippage_model({"slippage": "spread"}, asset_class="forex")
        assert isinstance(model, BarSpreadSlippage)

    def test_spread_with_unmapped_class_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            resolve_slippage_model({"slippage": "spread"}, asset_class="options")

    def test_unmapped_class_auto_records_none_and_warns(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="runner.slippage"):
            model = resolve_slippage_model({}, asset_class="options")
        assert isinstance(model, ZeroSlippage)
        assert model.describe()["reason"] == "unclassified_asset_class"
        assert any("options" in record.message for record in caplog.records)

    def test_unclassified_auto_records_none_and_warns(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="runner.slippage"):
            model = resolve_slippage_model({}, asset_class="")
        assert isinstance(model, ZeroSlippage)
        assert any(
            "asset class" in record.message for record in caplog.records
        )

    def test_external_spread_params_degrade_auto_to_fixed_bps(self) -> None:
        """spread_price_col / ask_ohlc already price the spread — the auto
        model must not charge the bar spread a second time."""
        for key in ("spread_price_col", "ask_ohlc"):
            model = resolve_slippage_model(
                {"slippage": "auto", key: "spread" if key == "spread_price_col" else True},
                asset_class="forex",
            )
            assert isinstance(model, FixedBpsSlippage), key
            assert model.bps == pytest.approx(1.0)

    def test_explicit_spread_with_external_spread_params_degrades(self) -> None:
        model = resolve_slippage_model(
            {"slippage": "spread", "spread_price_col": "spread"},
            asset_class="forex",
        )
        assert isinstance(model, FixedBpsSlippage)

    def test_nan_bps_rejected(self) -> None:
        with pytest.raises(ValueError, match="slippage_bps"):
            resolve_slippage_model({"slippage_bps": float("nan")}, asset_class="equities")
        with pytest.raises(ValueError):
            resolve_slippage_model({"slippage": "auto", "slippage_bps": float("nan")})


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
        # ask - bid = 0.4 price units beats the 50bps fallback (0.5). The
        # canonical OHLC is the BID series, so the spread is charged once —
        # on the buy side (MT5 convention): buys lift to the ask, sells
        # stay on the bid.
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.4)
        assert model.slip_price(100.0, "sell", bar) == pytest.approx(100.0)

    def test_bar_spread_even_count_median(self) -> None:
        # Four spread observations -> median is the mean of the middle two.
        model = BarSpreadSlippage(default_bps=50.0)
        bar = pd.Series(
            {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "ask_open": 100.1,  # 0.1
                "ask_high": 100.3,  # 0.3
                "ask_low": 100.5,  # 0.5
                "ask_close": 100.7,  # 0.7
            }
        )
        # sorted diffs [0.1, 0.3, 0.5, 0.7] -> (0.3 + 0.5) / 2 = 0.4
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.4)
        assert model.slip_price(100.0, "sell", bar) == pytest.approx(100.0)

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


def _forex_frame() -> pd.DataFrame:
    """Bid OHLC plus ``ask_*`` columns at a constant 0.4 spread; bar 15's
    high reaches 103 (LONG TP at 102) and bar 25's low reaches 97 (SHORT
    TP at 98)."""
    n = 30
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000.0] * n,
        }
    )
    frame.loc[15, "high"] = 103.0
    frame.loc[25, "low"] = 97.0
    for col in ("open", "high", "low", "close"):
        frame[f"ask_{col}"] = frame[col] + 0.4
    return frame


def _forex_strategy(*, direction: str = "LONG", tp: float = 102.0) -> Any:
    # A SHORT's protective stop must sit ABOVE the entry to be valid.
    stop = 50.0 if direction == "LONG" else 150.0

    def strategy_fn(df, params):
        n = len(df)
        entries = pd.Series([i == 10 for i in range(n)], index=df.index)
        exits = pd.Series(False, index=df.index)
        indicators = {
            "stop_loss": pd.Series([stop] * n, index=df.index),
            "take_profit": pd.Series([tp] * n, index=df.index),
        }
        if direction != "LONG":
            indicators["direction"] = pd.Series([direction] * n, index=df.index)
        return entries, exits, indicators, []

    return strategy_fn


class TestNativeEngineForexSpread:
    """End-to-end: the auto model resolves forex -> bar spread, and the
    MT5 bid/ask convention charges the spread once per round trip."""

    def test_long_entry_pays_ask_tp_sells_bid(self) -> None:
        from runner.engine import TradeEngine

        engine = TradeEngine(
            _engine_config({"fees": "zero"}),
            symbol="EURUSD",
            asset_class="forex",
        )
        result = engine.run(_forex_frame(), _forex_strategy())
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Entry fills next bar open (bid 100.0); the buy lifts to the ask.
        assert trade.entry_price == pytest.approx(100.4)
        # TP sells at the bid level: the spread is NOT subtracted again.
        tp_exit = trade.exits[0]
        assert tp_exit.reason == "tp"
        assert tp_exit.price == pytest.approx(102.0)

    def test_short_entry_sells_bid_exit_buys_ask(self) -> None:
        from runner.engine import TradeEngine

        engine = TradeEngine(
            _engine_config({"fees": "zero"}),
            symbol="EURUSD",
            asset_class="forex",
        )
        result = engine.run(
            _forex_frame(), _forex_strategy(direction="SHORT", tp=98.0)
        )
        trade = result.trades[0]
        # SHORT entry sells the bid: no spread on the sell side.
        assert trade.entry_price == pytest.approx(100.0)
        # SHORT TP buys the ask: 98 + 0.4.
        assert trade.exits[0].price == pytest.approx(98.4)


class TestReplayFlipSlippage:
    """The supertrend_flip workflow legs are fills like any other (#130)."""

    _FLIP_CONFIG = {
        "mode": "supertrend_flip",
        "breakeven_after_tp1": True,
        "activation_phase": "wait_for_first_green_after_tp1",
        "exit_phase": "exit_on_first_red_after_activation",
        "supertrend_period": 3,
        "supertrend_multiplier": 1.0,
    }
    _ENTRY_BAR = (100.0, 100.2, 99.8, 100.0)
    _TP1_BAR = (103.0, 110.5, 102.0, 108.0)

    def _frame(self, bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "timestamp": pd.date_range(
                    "2026-01-01T00:00:00Z", periods=len(bars), freq="1min"
                ),
                "open": [b[0] for b in bars],
                "high": [b[1] for b in bars],
                "low": [b[2] for b in bars],
                "close": [b[3] for b in bars],
                "volume": [1000] * len(bars),
            }
        )

    def _signal(self):
        from runner.engines.signal_replay_adapter import ReplaySignal

        return ReplaySignal.from_payload(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "symbol": "AAA",
                "action": "buy",
                "entry_price": 100.0,
                "stop_loss": 95.0,
                "take_profit": 110.0,
                "timeframe": "1m",
                "metadata": {
                    "trailing_sl_config": dict(self._FLIP_CONFIG),
                    "tp1_price": 110.0,
                    "tp1_exit_percent": 0.5,
                },
            },
            default_timeframe="1m",
        )

    def _adapter(self, **kwargs):
        from risk.config import RiskConfig
        from runner.engines.signal_replay_adapter import SignalReplayAdapter

        kwargs.setdefault("fee_params", {"fees": "zero"})
        kwargs.setdefault("initial_capital", 100_000.0)
        kwargs.setdefault(
            "risk_config",
            RiskConfig(
                max_risk_per_trade=0.01,
                max_total_exposure=0.5,
                max_positions=5,
                max_notional_per_trade=1.0,
            ),
        )
        kwargs.setdefault("default_timeframe", "1m")
        return SignalReplayAdapter(**kwargs)

    @staticmethod
    def _stub_supertrend(monkeypatch, states: str) -> None:
        from runner.engines import signal_replay_adapter as replay_module

        def _fake(df, period, multiplier):
            out = df.copy()
            out["supertrend"] = [s == "g" for s in states]
            out["supertrend_ready"] = [s != "n" for s in states]
            return out

        monkeypatch.setattr(replay_module, "calculate_supertrend", _fake)

    def _run(self, frame, adapter):
        return adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": frame},
            timeframe="1m",
            backtest_start="2026-01-01",
            backtest_end="2026-01-01",
        )

    def test_tp1_partial_leg_is_slipped(self, monkeypatch) -> None:
        flat = (102.0, 102.5, 101.5, 102.0)
        frame = self._frame([self._ENTRY_BAR, self._TP1_BAR, flat, flat])
        self._stub_supertrend(monkeypatch, "rrrr")
        adapter = self._adapter(slippage_params={"slippage_bps": 100.0})
        trade = self._run(frame, adapter).trades[0]

        tp1_leg = trade.exits[0]
        assert tp1_leg.reason == "tp1"
        # LONG sells lower by 1%: 110 -> 108.9.
        assert tp1_leg.price == pytest.approx(108.9)

    def test_flip_exit_slips_exactly_once(self, monkeypatch) -> None:
        # bars: 0 entry, 1 TP1, 2 green (activates), 3 red (flip), 4 fill
        flat = (102.0, 102.5, 101.5, 102.0)
        fill_bar = (104.0, 105.0, 101.0, 101.5)
        frame = self._frame(
            [self._ENTRY_BAR, self._TP1_BAR, flat, flat, fill_bar]
        )
        self._stub_supertrend(monkeypatch, "rggrg")
        adapter = self._adapter(slippage_params={"slippage_bps": 100.0})
        trade = self._run(frame, adapter).trades[0]

        flip_leg = trade.exits[-1]
        assert flip_leg.reason == "supertrend_flip"
        # Fill at the flip+1 bar OPEN (104.0), slipped once: 104 * 0.99.
        assert flip_leg.price == pytest.approx(104.0 * 0.99)


class TestSignalReplayEngineAssetClass:
    def test_asset_class_propagates_from_parquet_path(
        self, tmp_path: Path
    ) -> None:
        from runner.engines.signal_replay_adapter import SignalReplayEngine
        from tests.helpers import write_ohlcv

        write_ohlcv(tmp_path, "AAA", _engine_frame(), asset_class="equities")
        engine = SignalReplayEngine(
            data_dir=tmp_path, fee_params={"fees": "zero"}
        )
        result = engine.run_backtest(
            signals=[
                {
                    "timestamp": "2024-01-01T10:00:00Z",
                    "symbol": "AAA",
                    "action": "buy",
                    "entry_price": 100.0,
                    "stop_loss": 50.0,
                    "timeframe": "1h",
                }
            ],
            signal_processor=None,
            symbols=["AAA"],
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            timeframe="1h",
            auto_download=False,
        )
        model_meta = result.meta.data["slippage"]["models"]["AAA"]
        assert model_meta["asset_class"] == "equities"
        assert model_meta["model"] == "fixed_bps"
        # 5bps equity default on the 10:00 bar close (100.5): 100.5 * 1.0005.
        assert result.trades[0].entry_price == pytest.approx(100.5 * 1.0005)


class TestCliSlippageFlags:
    def test_auto_plus_bps_rejected_at_cli_layer(self) -> None:
        from types import SimpleNamespace

        import runner.cli as runner_cli

        args = SimpleNamespace(
            slippage="auto", slippage_bps=7.0, slippage_default_bps=None
        )
        with pytest.raises(ValueError, match="auto"):
            runner_cli._slippage_params_from_args(args)

    def test_none_plus_bps_rejected_at_cli_layer(self) -> None:
        from types import SimpleNamespace

        import runner.cli as runner_cli

        args = SimpleNamespace(
            slippage="none", slippage_bps=7.0, slippage_default_bps=None
        )
        with pytest.raises(ValueError, match="none/zero"):
            runner_cli._slippage_params_from_args(args)

    def test_valid_combinations_pass_through(self) -> None:
        from types import SimpleNamespace

        import runner.cli as runner_cli

        args = SimpleNamespace(
            slippage="bps", slippage_bps=7.0, slippage_default_bps=None
        )
        assert runner_cli._slippage_params_from_args(args) == {
            "slippage": "bps",
            "slippage_bps": 7.0,
        }
        args = SimpleNamespace(
            slippage=None, slippage_bps=None, slippage_default_bps=2.5
        )
        assert runner_cli._slippage_params_from_args(args) == {
            "slippage_default_bps": 2.5
        }


class TestViewerOhlcvAssetClass:
    def test_asset_class_comes_from_the_loaded_ohlcv_path(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from viewer import core

        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        path = (
            tmp_path
            / "ohlcv"
            / "dukascopy"
            / "forex"
            / "EURUSD"
            / "1h.parquet"
        )
        path.parent.mkdir(parents=True)
        _engine_frame().to_parquet(path, index=False)

        assert core.ohlcv_asset_class("EURUSD", "1h") == "forex"
        assert core.ohlcv_asset_class("MISSING", "1h") == ""
        assert core.ohlcv_asset_class("*", "1h") == ""
