"""Unit tests for runner.fees — pluggable fee/slippage cost models (#84)."""

from __future__ import annotations

import pytest

from runner.fees import (
    NotionalFeeModel,
    ZeroFeeModel,
    resolve_fee_model,
)


class TestResolveFeeModel:
    def test_missing_config_raises(self) -> None:
        """Missing fee/slippage config is a hard error, never silent gross."""
        with pytest.raises(ValueError, match="Missing fee/slippage config"):
            resolve_fee_model({})
        with pytest.raises(ValueError, match="Missing fee/slippage config"):
            resolve_fee_model(None)
        with pytest.raises(ValueError, match="Missing fee/slippage config"):
            resolve_fee_model({"unrelated": 1.0})

    def test_zero_declaration_resolves(self) -> None:
        model = resolve_fee_model({"fees": "zero"})
        assert isinstance(model, ZeroFeeModel)
        assert model.fee_for_trade(100.0, 101.0, 10.0) == 0.0

    def test_rate_keys_build_notional_model(self) -> None:
        model = resolve_fee_model({"fee_rate": 0.001, "fee_flat_per_trade": 2.0})
        assert isinstance(model, NotionalFeeModel)
        # (100 + 101) * 10 * 0.001 + 2.0 = 4.01
        assert model.fee_for_trade(100.0, 101.0, 10.0) == pytest.approx(4.01)

    def test_rate_only_builds_notional_model(self) -> None:
        model = resolve_fee_model({"fee_rate": 0.001})
        assert model.fee_for_trade(100.0, 100.0, 5.0) == pytest.approx(1.0)

    def test_explicit_model_passes_through(self) -> None:
        sentinel = ZeroFeeModel()
        assert resolve_fee_model({"fee_model": sentinel}) is sentinel

    def test_non_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="fee_for_trade"):
            resolve_fee_model({"fee_model": object()})

    def test_round_trip_only_model_rejected(self) -> None:
        """A model that cannot price a single leg is rejected, not half-used.

        An engine holding already-paid legs out of spendable cash
        (``TradeEngine._refresh_sizing_equity``) needs per-leg prices; a
        model without them must fail closed instead of mis-accounting.
        """

        class RoundTripOnly:
            def fee_for_trade(self, entry_price, exit_price, quantity):
                return 1.0

        with pytest.raises(ValueError, match="entry_fee"):
            resolve_fee_model({"fee_model": RoundTripOnly()})

    def test_fees_zero_conflicts_with_rate(self) -> None:
        # resolve itself accepts the keys (rate wins by precedence); the CLI
        # rejects the ambiguous combination — here rate keys take precedence.
        model = resolve_fee_model({"fees": "zero", "fee_rate": 0.001})
        assert isinstance(model, NotionalFeeModel)


class TestNotionalFeeModel:
    def test_negative_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            NotionalFeeModel(rate=-0.001)

    def test_negative_flat_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            NotionalFeeModel(flat_per_trade=-1.0)

    def test_uses_absolute_values(self) -> None:
        model = NotionalFeeModel(rate=0.001)
        assert model.fee_for_trade(-100.0, 101.0, -10.0) == pytest.approx(2.01)

    def test_legs_add_up_to_the_round_trip(self) -> None:
        """The contract engines rely on: ``fee_for_trade == entry + exit``.

        ``entry_fee`` carries the per-trade surcharge (the trade, and its
        surcharge, exists from the moment it is opened); ``exit_fee`` does not.
        """
        model = NotionalFeeModel(rate=0.001, flat_per_trade=2.0)
        # entry leg 0.001 * 100 * 10 + 2.0 surcharge = 3.0
        assert model.entry_fee(100.0, 10.0) == pytest.approx(3.0)
        # exit leg 0.001 * 101 * 10 = 1.01 (no second surcharge)
        assert model.exit_fee(101.0, 10.0) == pytest.approx(1.01)
        assert (
            model.entry_fee(100.0, 10.0) + model.exit_fee(101.0, 10.0)
            == pytest.approx(model.fee_for_trade(100.0, 101.0, 10.0))
        )

    def test_zero_model_prices_every_leg_at_zero(self) -> None:
        assert ZeroFeeModel().entry_fee(100.0, 10.0) == 0.0
        assert ZeroFeeModel().exit_fee(101.0, 10.0) == 0.0


class TestEngineHardError:
    def test_trading_engine_run_without_fee_config_raises(self) -> None:
        """End-to-end guard: an engine run without declared costs fails."""
        import pandas as pd

        from runner.engine import TradeEngine
        from runner.engines.base import BacktestConfig

        index = pd.RangeIndex(30)
        data = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=30, freq="h", tz="UTC"),
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.5] * 30,
                "volume": [1000.0] * 30,
            },
            index=index,
        )

        def strategy_fn(df, params):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[10] = True
            exits.iloc[20] = True
            return entries, exits, {}, []

        cfg = BacktestConfig(risk_per_trade_pct=0.01,
            strategy_name="unit-test",
            symbols=["TEST"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000,
            modeling_resolution="main_timeframe",
            # Slippage is declared so the run reaches the FEE gate this
            # test exercises; an undeclared slippage refuses even earlier.
            parameters={"slippage": "none"},
        )
        engine = TradeEngine(cfg, symbol="TEST")
        with pytest.raises(ValueError, match="Missing fee/slippage config"):
            engine.run(data, strategy_fn)
