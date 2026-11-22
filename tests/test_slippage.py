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

import uuid
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
    def test_unclassified_asset_class_refuses_under_auto(self) -> None:
        """Direct engine use (no asset class) fails closed unless the run
        declares slippage explicitly (maintainer decision 2026-11-22)."""
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model({}, asset_class="")
        model = resolve_slippage_model({"slippage": "none"}, asset_class="")
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

    def test_volatility_cfd_defaults_to_bar_spread(self) -> None:
        """Dukascopy ships VOLIDXUSD/DOLLARIDXUSD as ``asset_class:
        'volatility'`` bid/ask CFDs — same class family as energy/commodity,
        so auto must map them, not refuse (fail-closed stays for truly
        unmapped classes like 'options')."""
        model = resolve_slippage_model({}, asset_class="volatility")
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
        with pytest.raises(ValueError, match=r"slippage_bps|slippage_default_bps"):
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

    def test_unmapped_class_auto_refuses(self) -> None:
        """An unmapped asset class refuses the run under auto resolution —
        gross fills are never a silent default (fail closed like #84)."""
        with pytest.raises(ValueError, match="options"):
            resolve_slippage_model({}, asset_class="options")
        with pytest.raises(ValueError, match="options"):
            resolve_slippage_model({"slippage": "auto"}, asset_class="options")

    def test_unclassified_auto_refuses(self) -> None:
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model({}, asset_class="")

    def test_unmapped_class_explicit_slippage_is_allowed(self) -> None:
        """Every explicit declaration bypasses the auto refusal."""
        none_model = resolve_slippage_model(
            {"slippage": "none"}, asset_class="options"
        )
        assert isinstance(none_model, ZeroSlippage)
        assert none_model.describe()["reason"] == "explicit_opt_out"
        bps_model = resolve_slippage_model(
            {"slippage": "bps", "slippage_bps": 4.0}, asset_class="options"
        )
        assert isinstance(bps_model, FixedBpsSlippage)
        assert bps_model.bps == pytest.approx(4.0)
        spread_model = resolve_slippage_model(
            {"slippage": "spread", "slippage_bps": 4.0}, asset_class="options"
        )
        assert isinstance(spread_model, BarSpreadSlippage)
        assert spread_model.default_bps == pytest.approx(4.0)

    def test_external_spread_params_degrade_auto_to_fixed_bps(self) -> None:
        """spread_price_col already prices the spread — the auto model must
        not charge the bar spread a second time. ``ask_ohlc`` does NOT
        degrade: it only moves SHORT triggers onto the ask side, and
        ask-triggered exits pass ``fill_side="ask"`` instead."""
        model = resolve_slippage_model(
            {"slippage": "auto", "spread_price_col": "spread"},
            asset_class="forex",
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(1.0)

    def test_ask_ohlc_keeps_bar_spread_model(self) -> None:
        model = resolve_slippage_model(
            {"slippage": "auto", "ask_ohlc": True}, asset_class="forex"
        )
        assert isinstance(model, BarSpreadSlippage)

    def test_ask_triggered_short_exit_does_not_double_charge_spread(self) -> None:
        """``fill_side="ask"`` marks an ask-denominated fill: the bar spread
        is skipped, but the bps fallback still applies when the bar has no
        ask columns."""
        model = BarSpreadSlippage(default_bps=10.0)
        bar = pd.Series({"open": 100.0, "close": 100.0, "ask_close": 100.4})
        assert model.slip_price(102.0, "buy", bar, fill_side="ask") == pytest.approx(102.0)
        # Without ask columns the fallback bps applies symmetrically.
        plain = pd.Series({"open": 100.0, "close": 100.0})
        assert model.slip_price(100.0, "buy", plain, fill_side="ask") == pytest.approx(100.1)
        # fill_side never affects sells or non-ask buys.
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.4)
        assert model.slip_price(100.0, "sell", bar, fill_side="ask") == pytest.approx(100.0)

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

    def test_inf_bps_rejected(self) -> None:
        with pytest.raises(ValueError, match="slippage"):
            resolve_slippage_model({"slippage_bps": float("inf")}, asset_class="equities")
        with pytest.raises(ValueError):
            resolve_slippage_model({"slippage": float("-inf")}, asset_class="equities")

    def test_none_spelling_with_slippage_bps_rejected(self) -> None:
        """``{"slippage":"none","slippage_bps":10}`` must not silently drop
        the declared bps — same conflict the CLI layer rejects."""
        for spelling in ("none", "zero", "off", "parity", "no"):
            with pytest.raises(ValueError, match="slippage_bps"):
                resolve_slippage_model(
                    {"slippage": spelling, "slippage_bps": 10.0},
                    asset_class="equities",
                )

    def test_none_spelling_with_tuning_keys_rejected(self) -> None:
        for extra in (
            {"slippage_default_bps": 5.0},
            {"slippage_defaults": {"equities": 5.0}},
        ):
            with pytest.raises(ValueError, match="slippage"):
                resolve_slippage_model(
                    {"slippage": "none", **extra}, asset_class="equities"
                )

    def test_non_string_non_numeric_slippage_rejected(self) -> None:
        """``slippage`` that is neither a spelling nor a number must raise,
        not fall through to the auto model."""
        for bad in (True, {"bps": 10}, ["bps"], object()):
            with pytest.raises(ValueError, match="slippage"):
                resolve_slippage_model({"slippage": bad}, asset_class="equities")

    def test_numeric_slippage_with_tuning_keys_rejected(self) -> None:
        """``{"slippage":7,"slippage_bps":20}`` must not silently keep 7."""
        for extra in (
            {"slippage_bps": 20.0},
            {"slippage_default_bps": 20.0},
            {"slippage_defaults": {"equities": 20.0}},
        ):
            with pytest.raises(ValueError, match="slippage"):
                resolve_slippage_model({"slippage": 7, **extra}, asset_class="equities")

    def test_spread_spelling_uses_slippage_bps_as_fallback(self) -> None:
        """``slippage='spread'`` + ``slippage_bps``: the bps is the bar-less
        fallback, not a conflicting declaration."""
        model = resolve_slippage_model(
            {"slippage": "spread", "slippage_bps": 10.0}, asset_class="forex"
        )
        assert isinstance(model, BarSpreadSlippage)
        assert model.default_bps == pytest.approx(10.0)
        bar = pd.Series({"open": 100.0, "close": 100.0})
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.1)

    def test_bps_spelling_without_bps_uses_class_default(self) -> None:
        model = resolve_slippage_model({"slippage": "bps"}, asset_class="equities")
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(5.0)

    def test_per_class_defaults_beat_global_default_bps(self) -> None:
        model = resolve_slippage_model(
            {
                "slippage_defaults": {"equities": 20.0},
                "slippage_default_bps": 3.0,
            },
            asset_class="equities",
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(20.0)
        # ...and the global default still applies to other classes.
        other = resolve_slippage_model(
            {
                "slippage_defaults": {"equities": 20.0},
                "slippage_default_bps": 3.0,
            },
            asset_class="crypto",
        )
        assert isinstance(other, FixedBpsSlippage)
        assert other.bps == pytest.approx(3.0)

    def test_slippage_defaults_keys_are_normalized(self) -> None:
        """``{"Equities": 20}`` must apply to ``equities`` — config names
        normalize like data columns (strip + lowercase)."""
        model = resolve_slippage_model(
            {"slippage_defaults": {" Equities ": 20.0}}, asset_class="equities"
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(20.0)

    def test_slippage_defaults_unknown_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model(
                {"slippage_defaults": {"options": 5.0}}, asset_class="equities"
            )
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model(
                {"slippage_defaults": {"not_a_class": 5.0}}, asset_class="crypto"
            )

    def test_slippage_defaults_conflicting_normalized_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="conflicting"):
            resolve_slippage_model(
                {"slippage_defaults": {"Equities": 20.0, "equities": 30.0}},
                asset_class="equities",
            )
        # Agreeing duplicates are harmless.
        model = resolve_slippage_model(
            {"slippage_defaults": {"Equities": 20.0, "equities": 20.0}},
            asset_class="equities",
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(20.0)

    def test_slippage_defaults_non_dict_rejected(self) -> None:
        with pytest.raises(ValueError, match="slippage_defaults"):
            resolve_slippage_model(
                {"slippage_defaults": ["equities"]}, asset_class="equities"
            )

    def test_fixed_bps_rejects_dead_fallback_keys(self) -> None:
        """``slippage_bps`` fixes every asset class, so ``slippage_default_bps``
        / ``slippage_defaults`` beside it are silently dead config — reject,
        the same rule ``slippage=<number>`` already follows."""
        for extra in (
            {"slippage_default_bps": 2.0},
            {"slippage_defaults": {"forex": 2.0}},
        ):
            with pytest.raises(ValueError, match="slippage"):
                resolve_slippage_model(
                    {"slippage_bps": 3.0, **extra}, asset_class="forex"
                )
        # Same under the explicit 'bps' and 'spread' spellings (where
        # slippage_bps is the fixed value / the bar-less fallback).
        for spelling in ("bps", "spread"):
            with pytest.raises(ValueError, match="slippage"):
                resolve_slippage_model(
                    {
                        "slippage": spelling,
                        "slippage_bps": 3.0,
                        "slippage_defaults": {"forex": 2.0},
                    },
                    asset_class="forex",
                )

    def test_slippage_defaults_validated_on_gross_and_fixed_paths(self) -> None:
        """A typo'd ``slippage_defaults`` key must raise even when the run
        never reaches ``_class_default_bps``: an unclassified asset resolves
        gross, and a fixed ``slippage_bps`` never consults the fallbacks."""
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model(
                {"slippage_defaults": {"equitys": 20.0}}, asset_class=""
            )
        with pytest.raises(ValueError, match="asset class"):
            resolve_slippage_model(
                {
                    "slippage": "bps",
                    "slippage_bps": 3.0,
                    "slippage_defaults": {"bogus": 1.0},
                },
                asset_class="equities",
            )

    def test_fixed_bps_ignores_an_empty_slippage_defaults_dict(self) -> None:
        """``slippage_defaults: {}`` configures nothing, so beside a fixed
        ``slippage_bps`` it is not dead config and must not trip the
        rejection the populated dict does."""
        model = resolve_slippage_model(
            {"slippage_bps": 3.0, "slippage_defaults": {}}, asset_class="forex"
        )
        assert isinstance(model, FixedBpsSlippage)
        assert model.bps == pytest.approx(3.0)


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

    def test_bar_spread_odd_count_median(self) -> None:
        """Three spread observations -> the middle value, not an average
        of neighbours (kills the even-count-only median mutant)."""
        model = BarSpreadSlippage(default_bps=50.0)
        bar = pd.Series(
            {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                # close has no ask counterpart -> 3 diffs: 0.1, 0.3, 0.5
                "ask_open": 100.1,
                "ask_high": 100.3,
                "ask_low": 100.5,
            }
        )
        assert model.slip_price(100.0, "buy", bar) == pytest.approx(100.3)
        assert model.slip_price(100.0, "sell", bar) == pytest.approx(100.0)

    def test_bar_spread_negative_default_bps_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            BarSpreadSlippage(default_bps=-0.5)


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

    def test_short_level_exit_gapped_at_open_slips_once(self) -> None:
        """#354 review SHORT mirror: a ``level`` exit whose level sits beyond
        the whole exit bar is marketable at the open — the fill base is the
        bar OPEN and slippage applies exactly once on top (buy side, up)."""
        from runner.engine import TradeEngine

        frame = _engine_frame()
        # Exit bar (index 20): the whole bar trades below the level exit
        # price, so the resting buy is marketable at the open.
        frame.loc[20, ["open", "high", "low", "close"]] = [104.0, 104.5, 103.5, 104.2]

        def short_level_strategy(df, params):
            n = len(df)
            entries = pd.Series([i == 10 for i in range(n)], index=df.index)
            exits = pd.Series([i == 20 for i in range(n)], index=df.index)
            indicators = {
                "direction": pd.Series("SHORT", index=df.index),
                "stop_loss": pd.Series([150.0] * n, index=df.index),
                "take_profit": pd.Series([50.0] * n, index=df.index),
                "exit_order_type": pd.Series(["level"] * n, index=df.index),
                "exit_price": pd.Series([105.0] * n, index=df.index),
            }
            return entries, exits, indicators, []

        engine = TradeEngine(
            _engine_config({"slippage_bps": 10.0}),  # 0.1%
            symbol="TEST",
        )
        result = engine.run(frame, short_level_strategy)
        assert len(result.trades) == 1
        trade = result.trades[0]

        leg = trade.exits[0]
        assert leg.reason == "signal"
        # Gap base = bar open 104.0 (no ask columns -> no bar spread);
        # SHORT buy slips UP once: 104 * 1.001.
        assert leg.price == pytest.approx(104.0 * 1.001)
        assert leg.pnl == pytest.approx(
            (trade.entry_price - leg.price) * leg.quantity
        )
        assert trade.meta["exit_level_gapped"] is True


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

    def test_price_basis_reflects_resolved_model_on_two_sided_data(
        self, tmp_path: Path
    ) -> None:
        """``price_basis`` is 'bid_ask' only when the bar-spread model can
        use the ask columns; 'bid' when they exist but the model is none."""
        from runner.engines.base import BacktestConfig
        from runner.engines.native_adapter import NativeAdapter
        from tests.helpers import write_ohlcv

        strategy_path = tmp_path / "strat.py"
        strategy_path.write_text(
            "import pandas as pd\n"
            "def generate_signals(df, params):\n"
            "    n = len(df)\n"
            "    entries = pd.Series([i == 10 for i in range(n)], index=df.index)\n"
            "    exits = pd.Series([i == 20 for i in range(n)], index=df.index)\n"
            "    return entries, exits, {}, []\n"
        )

        def _config(parameters):
            return BacktestConfig(
                strategy_name="slip",
                symbols=["EURUSD"],
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-01-31",
                initial_capital=100_000,
                risk_per_trade_pct=0.01,
                modeling_resolution="main_timeframe",
                data_dir=str(tmp_path),
                parameters=parameters,
            )

        write_ohlcv(tmp_path, "EURUSD", _forex_frame(), asset_class="forex")

        spread_result = NativeAdapter().run(
            _config({"fees": "zero"}), str(strategy_path)
        )
        model_meta = spread_result.meta.data["slippage"]["models"]["EURUSD"]
        assert model_meta["model"] == "bar_spread"
        assert model_meta["price_basis"] == "bid_ask"

        none_result = NativeAdapter().run(
            _config({"fees": "zero", "slippage": "none"}), str(strategy_path)
        )
        model_meta = none_result.meta.data["slippage"]["models"]["EURUSD"]
        assert model_meta["model"] == "none"
        assert model_meta["price_basis"] == "bid"


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

    def test_metadata_slippage_params_override_adapter_params(self) -> None:
        """``metadata["slippage_params"]`` must win over the constructor
        params — the run-level declaration is the most specific one."""
        adapter = self._adapter(
            fee_params={"fees": "zero"}, slippage_params={"slippage_bps": 100.0}
        )
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": _engine_frame()},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
            # 50bps overrides the constructor's 100bps (the merge keeps
            # the declaration consistent: 'bps' consumes slippage_bps).
            metadata={"slippage_params": {"slippage": "bps", "slippage_bps": 50.0}},
        )
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(100.5 * 1.005)
        model_meta = result.meta.data["slippage"]["models"]["AAA"]
        assert model_meta["model"] == "fixed_bps"
        assert model_meta["bps"] == pytest.approx(50.0)

    def test_metadata_slippage_params_conflict_is_rejected(self) -> None:
        """A metadata override that contradicts the constructor params
        (``none`` vs a declared bps) must fail closed, not silently pick
        one side — same rule as the params-level resolution."""
        adapter = self._adapter(
            fee_params={"fees": "zero"}, slippage_params={"slippage_bps": 100.0}
        )
        with pytest.raises(ValueError, match="slippage"):
            adapter.run(
                signals=[self._signal()],
                ohlcv_by_symbol={"AAA": _engine_frame()},
                timeframe="1h",
                backtest_start="2024-01-01",
                backtest_end="2024-01-02",
                metadata={"slippage_params": {"slippage": "none"}},
            )

    def test_explicit_slippage_model_object_is_used(self) -> None:
        adapter = self._adapter(
            fee_params={"fees": "zero"},
            slippage_model=FixedBpsSlippage(bps=50.0),
        )
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": _engine_frame()},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
        )
        trade = result.trades[0]
        # 50bps on the 10:00 bar close (100.5): 100.5 * 1.005.
        assert trade.entry_price == pytest.approx(100.5 * 1.005)
        model_meta = result.meta.data["slippage"]["models"]["AAA"]
        assert model_meta["model"] == "fixed_bps"
        assert model_meta["bps"] == pytest.approx(50.0)

    def test_price_basis_bid_ask_only_when_bar_spread_model(self) -> None:
        """``price_basis`` must describe the basis the resolved model
        prices on — 'bid_ask' only for the bar-spread model, 'bid' when
        ask columns exist but the model does not use them."""
        forex_frame = _forex_frame()
        # Bar-spread model on ask data -> bid_ask.
        adapter = self._adapter(
            fee_params={"fees": "zero"},
            slippage_params={"slippage": "spread", "slippage_bps": 5.0},
            asset_class_by_symbol={"AAA": "forex"},
        )
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": forex_frame},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
        )
        meta = result.meta.data["slippage"]["models"]["AAA"]
        assert meta["model"] == "bar_spread"
        assert meta["price_basis"] == "bid_ask"

        # Explicit none on the same ask data -> fills on the bid series.
        adapter = self._adapter(
            fee_params={"fees": "zero"},
            slippage_params={"slippage": "none"},
            asset_class_by_symbol={"AAA": "forex"},
        )
        result = adapter.run(
            signals=[self._signal()],
            ohlcv_by_symbol={"AAA": forex_frame},
            timeframe="1h",
            backtest_start="2024-01-01",
            backtest_end="2024-01-02",
        )
        meta = result.meta.data["slippage"]["models"]["AAA"]
        assert meta["model"] == "none"
        assert meta["price_basis"] == "bid"


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

    def test_long_stop_loss_sells_bid_level(self) -> None:
        """A LONG stop exits on the bid: the bar spread is NOT subtracted
        again on the sell side (pins the exit-bar plumbing mutant)."""
        from runner.engine import TradeEngine

        def stop_strategy(df, params):
            n = len(df)
            entries = pd.Series([i == 10 for i in range(n)], index=df.index)
            exits = pd.Series(False, index=df.index)
            indicators = {
                "stop_loss": pd.Series([98.0] * n, index=df.index),
                "take_profit": pd.Series([200.0] * n, index=df.index),
            }
            return entries, exits, indicators, []

        engine = TradeEngine(
            _engine_config({"fees": "zero"}),
            symbol="EURUSD",
            asset_class="forex",
        )
        result = engine.run(_forex_frame(), stop_strategy)
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Entry buys the ask: 100.0 + 0.4. Stop 98 triggers on bar 25
        # (bid low 97); the sell stays on the bid — 98.0 exactly, the
        # spread is not charged a second time on the exit side.
        assert trade.entry_price == pytest.approx(100.4)
        assert trade.exits[0].reason == "stop_loss"
        assert trade.exits[0].price == pytest.approx(98.0)

    def test_short_stop_loss_buys_ask_level_plus_spread(self) -> None:
        from runner.engine import TradeEngine

        def stop_strategy(df, params):
            n = len(df)
            entries = pd.Series([i == 10 for i in range(n)], index=df.index)
            exits = pd.Series(False, index=df.index)
            indicators = {
                "direction": pd.Series(["SHORT"] * n, index=df.index),
                "stop_loss": pd.Series([102.0] * n, index=df.index),
                "take_profit": pd.Series([50.0] * n, index=df.index),
            }
            return entries, exits, indicators, []

        engine = TradeEngine(
            _engine_config({"fees": "zero"}),
            symbol="EURUSD",
            asset_class="forex",
        )
        result = engine.run(_forex_frame(), stop_strategy)
        assert len(result.trades) == 1
        trade = result.trades[0]
        # SHORT entry sells the bid at 100.0 (no spread on sells).
        assert trade.entry_price == pytest.approx(100.0)
        # Stop 102 triggers on bar 15 (bid high 103); the buy-back lifts
        # to the ask: 102 + 0.4 spread — charged once via the slippage
        # model, not again on the sell side.
        assert trade.exits[0].reason == "stop_loss"
        assert trade.exits[0].price == pytest.approx(102.4)

    def test_ask_ohlc_short_stop_does_not_double_charge_spread(self) -> None:
        """``ask_ohlc`` keeps the bar-spread model: an ask-triggered SHORT
        stop fills at the ask-denominated level WITHOUT paying the spread
        a second time, while a LONG entry still pays it once."""
        from runner.engine import TradeEngine

        def stop_strategy(df, params):
            n = len(df)
            entries = pd.Series([i == 10 for i in range(n)], index=df.index)
            exits = pd.Series(False, index=df.index)
            indicators = {
                "direction": pd.Series(["SHORT"] * n, index=df.index),
                # 103.2 sits between the bar's bid high (103.0) and ask
                # high (103.4): only the ask side can trigger it.
                "stop_loss": pd.Series([103.2] * n, index=df.index),
                "take_profit": pd.Series([50.0] * n, index=df.index),
            }
            return entries, exits, indicators, []

        engine = TradeEngine(
            _engine_config({"fees": "zero", "ask_ohlc": True}),
            symbol="EURUSD",
            asset_class="forex",
        )
        result = engine.run(_forex_frame(), stop_strategy)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exits[0].reason == "stop_loss"
        # Ask-triggered level is already ask-denominated: 103.2 exactly,
        # not 103.2 + 0.4.
        assert trade.exits[0].price == pytest.approx(103.2)


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

    # ── gap-at-open × slippage composition (#354 review) ─────────────────
    #
    # #364 made protective exits gap-fill at the bar's OPEN; #130 slips the
    # fill price once. The two must compose exactly once on every gap leg:
    # exit.price == gap_base * (1 -/+ bps). These pin that a later edit
    # cannot slip the base twice, skip the slip on a gap path, or slip the
    # pre-gap level instead of the open. The workflow is long-only, so the
    # SHORT mirror lives in TestNativeEngineFillPriceSlippage (the native
    # ``level`` exit's gap-at-open fill).

    _GAP_BPS = 10.0

    def _gap_adapter(self):
        return self._adapter(slippage_params={"slippage_bps": self._GAP_BPS})

    def test_gap_through_stop_slips_the_gap_open_once(self, monkeypatch) -> None:
        flat = (102.0, 102.5, 101.5, 102.0)
        gap_stop_bar = (94.0, 94.5, 93.0, 94.2)
        frame = self._frame([self._ENTRY_BAR, flat, gap_stop_bar, flat])
        self._stub_supertrend(monkeypatch, "gggg")
        trade = self._run(frame, self._gap_adapter()).trades[0]

        leg = trade.exits[0]
        assert leg.reason == "stop_loss"
        # LONG sells lower: base is the gap OPEN (94), not the 95 level.
        assert leg.price == pytest.approx(94.0 * (1 - self._GAP_BPS / 1e4))
        assert leg.pnl == pytest.approx(
            (leg.price - trade.entry_price) * leg.quantity
        )

    def test_gap_through_tp1_slips_the_gap_open_once(self, monkeypatch) -> None:
        flat = (102.0, 102.5, 101.5, 102.0)
        gap_tp1_bar = (111.0, 112.0, 110.5, 111.5)
        frame = self._frame([self._ENTRY_BAR, gap_tp1_bar, flat, flat])
        self._stub_supertrend(monkeypatch, "rrrr")
        trade = self._run(frame, self._gap_adapter()).trades[0]

        tp1_leg = trade.exits[0]
        assert tp1_leg.reason == "tp1"
        assert tp1_leg.price == pytest.approx(111.0 * (1 - self._GAP_BPS / 1e4))
        assert tp1_leg.pnl == pytest.approx(
            (tp1_leg.price - trade.entry_price) * tp1_leg.quantity
        )

    def test_gap_through_breakeven_stop_slips_the_gap_open_once(
        self, monkeypatch
    ) -> None:
        flat = (102.0, 102.5, 101.5, 102.0)
        gap_bar = (99.0, 99.5, 98.0, 98.5)
        frame = self._frame([self._ENTRY_BAR, self._TP1_BAR, flat, gap_bar, flat])
        self._stub_supertrend(monkeypatch, "rrrrr")
        trade = self._run(frame, self._gap_adapter()).trades[0]

        remainder = trade.exits[1]
        assert remainder.reason == "breakeven_stop"
        assert remainder.price == pytest.approx(99.0 * (1 - self._GAP_BPS / 1e4))
        assert remainder.pnl == pytest.approx(
            (remainder.price - trade.entry_price) * remainder.quantity
        )

    def test_gap_tp1_then_level_stop_each_slip_once(self, monkeypatch) -> None:
        """The TP1 tranche gap-fills at the open; the remainder's pre-TP1
        stop still working that bar fills AT THE LEVEL (95) — and each leg
        is slipped exactly once."""
        flat = (102.0, 102.5, 101.5, 102.0)
        gap_tp1_then_stop = (111.0, 112.0, 94.0, 105.0)
        frame = self._frame([self._ENTRY_BAR, gap_tp1_then_stop, flat, flat])
        self._stub_supertrend(monkeypatch, "rrrr")
        trade = self._run(frame, self._gap_adapter()).trades[0]

        tp1_leg, remainder = trade.exits
        assert tp1_leg.reason == "tp1"
        assert tp1_leg.price == pytest.approx(111.0 * (1 - self._GAP_BPS / 1e4))
        assert remainder.reason == "stop_loss"
        assert remainder.price == pytest.approx(95.0 * (1 - self._GAP_BPS / 1e4))


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

    def test_mixed_asset_class_matches_are_unclassified(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``_load_ohlcv_df`` merges every selected match; when they span
        more than one asset class there is no single correct class, so the
        symbol must classify as ``""`` (explicit ``none`` downstream)."""
        from tests.helpers import write_ohlcv
        from viewer import core

        monkeypatch.setattr(core, "DATA_DIR", tmp_path)
        frame = _engine_frame()
        write_ohlcv(tmp_path, "MIXED", frame, asset_class="equities")
        write_ohlcv(tmp_path, "MIXED", frame, asset_class="crypto")

        assert core.ohlcv_asset_class("MIXED", "1h") == ""


class TestViewerSlippageWiring:
    """Endpoint-level coverage for the #130 viewer replay wiring: the
    replay must classify each symbol from its OHLCV path and forward any
    declared slippage params to ``SignalReplayAdapter``."""

    @pytest.fixture
    def replay_client(self, monkeypatch, tmp_path: Path):
        from fastapi.testclient import TestClient

        import viewer.main as viewer_app_module
        from viewer import core as legacy_module

        for module in (viewer_app_module, legacy_module):
            monkeypatch.setattr(module, "DATA_DIR", tmp_path)
            monkeypatch.setattr(module, "BACKTESTS_DIR", tmp_path / "backtests")
            monkeypatch.setattr(module, "LOGS_DIR", tmp_path / "logs")
            monkeypatch.setattr(
                module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks"
            )
            monkeypatch.setattr(
                module, "OPTIMIZATIONS_DIR", tmp_path / "optimizations"
            )
            monkeypatch.setattr(module, "PORTFOLIOS_DIR", tmp_path / "portfolios")
        viewer_app_module.DATA_TASKS.clear()

        captured: dict[str, Any] = {}

        class _FakeAdapter:
            def __init__(self, **kwargs):
                captured["adapter_kwargs"] = kwargs

            def run(self, **_kwargs):
                from shared.models import (
                    BacktestMeta,
                    BacktestResult,
                    EquityPoint,
                )

                meta = BacktestMeta(
                    id=f"replay-{uuid.uuid4().hex[:8]}",
                    created_at="2026-03-11T10:00:00Z",
                    engine="native",
                    strategies=["signal replay"],
                    backtest_start="2024-01-01",
                    backtest_end="2024-01-10",
                    initial_capital=100000.0,
                    duration_days=9,
                    data={"symbols": ["BTCUSDT"], "timeframes": ["1h"]},
                )
                return BacktestResult(
                    meta=meta,
                    trades=[],
                    equity_curve=[
                        EquityPoint(
                            time="2024-01-02T00:00:00Z", equity=100000.0
                        )
                    ],
                )

        monkeypatch.setattr(
            viewer_app_module, "SignalReplayAdapter", _FakeAdapter
        )
        monkeypatch.setattr(
            viewer_app_module, "save_result_to_sqlite", lambda _r: None
        )
        monkeypatch.setattr(
            viewer_app_module, "_upsert_backtest_meta_cache", lambda _i: None
        )
        return TestClient(viewer_app_module.app), captured

    @staticmethod
    def _wait_for_task(client, task_id: str) -> dict:
        import time

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            response = client.get(f"/api/backtests/run/{task_id}/status")
            body = response.json()
            if body.get("status") in {"completed", "failed"}:
                return body
            time.sleep(0.05)
        raise AssertionError(f"replay task {task_id} did not finish")

    def _post_replay(self, client, tmp_path: Path, **slippage):
        from tests.helpers import write_ohlcv

        write_ohlcv(
            tmp_path,
            "BTCUSDT",
            _engine_frame(),
            source="yfinance",
            asset_class="crypto",
        )
        payload = {
            "signals": [
                {
                    "timestamp": "2024-01-02T10:00:00Z",
                    "symbol": "BTCUSDT",
                    "action": "buy",
                    "entry_price": 100.0,
                    "timeframe": "1h",
                }
            ],
            "start_date": "2024-01-01",
            "end_date": "2024-01-10",
            "timeframe": "1h",
            "capital": 100000.0,
            "auto_download": False,
            # #361/#84: the replay adapter fails closed without an explicit
            # fee declaration; these tests pin slippage, not fees.
            "fees_zero": True,
            **slippage,
        }
        response = client.post("/api/backtests/run-signals", json=payload)
        assert response.status_code == 202, response.text
        body = self._wait_for_task(client, response.json()["task_id"])
        assert body.get("status") == "completed", body.get("error")
        return body

    def test_run_signals_forwards_class_and_slippage_params(
        self, replay_client, tmp_path: Path
    ) -> None:
        client, captured = replay_client

        self._post_replay(
            client,
            tmp_path,
            slippage="bps",
            slippage_bps=7.5,
            slippage_default_bps=2.5,
        )

        adapter_kwargs = captured["adapter_kwargs"]
        assert adapter_kwargs["asset_class_by_symbol"] == {
            "BTCUSDT": "crypto"
        }
        assert adapter_kwargs["slippage_params"] == {
            "slippage": "bps",
            "slippage_bps": 7.5,
            "slippage_default_bps": 2.5,
        }

    def test_run_signals_without_slippage_sends_empty_params(
        self, replay_client, tmp_path: Path
    ) -> None:
        client, captured = replay_client

        self._post_replay(client, tmp_path)

        adapter_kwargs = captured["adapter_kwargs"]
        assert adapter_kwargs["asset_class_by_symbol"] == {
            "BTCUSDT": "crypto"
        }
        assert adapter_kwargs["slippage_params"] == {}

    def test_run_backtest_from_signals_forwards_slippage_fields(
        self, monkeypatch, replay_client, tmp_path: Path
    ) -> None:
        import viewer.main as viewer_app_module

        client, _captured = replay_client
        forwarded: list[Any] = []

        async def _fake_run_signals(payload):
            forwarded.append(payload)
            return {"task_id": "fake", "job_id": "fake", "status": "queued"}

        monkeypatch.setattr(
            viewer_app_module, "run_backtest_signals", _fake_run_signals
        )

        slippage = {
            "slippage": "bps",
            "slippage_bps": 7.5,
            "slippage_default_bps": 2.5,
        }
        response = client.post(
            "/api/backtests/run-from-signals",
            json={
                "signal_source": "discord-history",
                "start_date": "2024-01-01",
                "end_date": "2024-01-10",
                "timeframe": "1h",
                **slippage,
            },
        )
        assert response.status_code == 202, response.text

        csv_path = tmp_path / "signals" / "replay.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(
            "Symbol;Signal Price;Datum;Zeit;Signal Type\n"
            "AAPL;173,79;2026-02-23;09:32:00-05:00;BUY\n",
            encoding="utf-8",
        )
        response = client.post(
            "/api/backtests/run-from-signals",
            json={
                "signal_source": "csv",
                "filepath": str(csv_path),
                "start_date": "2026-02-23",
                "end_date": "2026-02-24",
                **slippage,
            },
        )
        assert response.status_code == 202, response.text

        assert len(forwarded) == 2
        for payload in forwarded:
            assert payload.slippage == "bps"
            assert payload.slippage_bps == 7.5
            assert payload.slippage_default_bps == 2.5
